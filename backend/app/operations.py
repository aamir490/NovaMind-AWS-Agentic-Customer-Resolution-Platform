"""Read-only local business operations using an explicit demonstration policy."""

from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from .catalog_seed import PRODUCT_SEEDS
from .domain import Name, Record
from .service import CaseService, ConflictError, NotFoundError
from .observability import observed


class ReturnReason(StrEnum):
    WRONG_ITEM = "wrong_item"
    DAMAGED = "damaged"
    CHANGE_OF_MIND = "change_of_mind"


class ItemCondition(StrEnum):
    UNUSED = "unused"
    USED = "used"
    DAMAGED = "damaged"


class ReturnPolicy(Record):
    id: Name
    version: Name
    return_window_days: int = Field(ge=0)
    allowed_reasons: tuple[ReturnReason, ...]
    change_of_mind_requires_unused: bool
    refund_requires_return: bool
    description: str


class InventoryItem(Record):
    sku: Name
    available_quantity: int = Field(ge=0)
    policy_id: Name


class EligibilityResult(Record):
    evidence_id: UUID | None = None
    case_id: UUID | None = None
    order_id: UUID
    customer_id: UUID
    sku: Name
    requested_quantity: int
    purchased_quantity: int
    days_since_delivery: int
    reason: ReturnReason
    condition: ItemCondition
    policy_id: Name
    policy_version: Name
    return_eligible: bool
    refund_eligible: bool
    denial_reasons: tuple[str, ...]
    refund_requires_return: bool
    assessment_basis: Literal["local_scenario_unverified_inputs", "trusted_delivery_record"] = "local_scenario_unverified_inputs"
    authorization_granted: Literal[False] = False


class EligibilityInput(Record):
    customer_id: UUID
    sku: Name
    quantity: int = Field(strict=True, ge=1, le=1000)
    days_since_delivery: int = Field(strict=True, ge=0, le=36500)
    reason: ReturnReason
    condition: ItemCondition


class EligibilityEvidence(Record):
    """Server-issued, immutable evidence for one case/order/item assessment."""

    id: UUID
    case_id: UUID
    order_id: UUID
    customer_id: UUID
    sku: Name
    delivery_fact_id: UUID
    delivery_source_id: Name
    delivery_source_reference: Name
    result: EligibilityResult
    created_at: datetime


class AuthoritativeDeliveryFact(Record):
    """A trusted-host delivery/return verification record; never accepted from HTTP or tools."""

    id: UUID
    case_id: UUID
    order_id: UUID
    customer_id: UUID
    sku: Name
    quantity: int = Field(strict=True, ge=1, le=1000)
    delivered_at: datetime
    reason: ReturnReason
    condition: ItemCondition
    source_id: Name
    source_reference: Name
    verified_at: datetime


class TrustedDeliverySource(Record):
    """A trusted-host configured fulfillment or return-verification source."""

    id: Name


class RefundEvidenceRepository(Protocol):
    """Optional durable boundary for trusted refund inputs and issued evidence."""

    def add_trusted_source(self, source: TrustedDeliverySource) -> None: ...
    def store_delivery_fact(self, fact: AuthoritativeDeliveryFact) -> AuthoritativeDeliveryFact: ...
    def get_delivery_fact(self, case_id: UUID, sku: str) -> AuthoritativeDeliveryFact | None: ...
    def store_eligibility_evidence(self, evidence: EligibilityEvidence) -> EligibilityEvidence: ...
    def get_eligibility_evidence(self, evidence_id: UUID) -> EligibilityEvidence: ...


class DeliveryFactIngestion:
    """Server-only boundary for records delivered by configured trusted sources."""

    def __init__(self, operations: "BusinessOperations",
                 sources: tuple[TrustedDeliverySource, ...] = ()) -> None:
        self._operations = operations
        self._sources = {source.id: source for source in sources}
        if len(self._sources) != len(sources):
            raise ValueError("Trusted delivery source IDs must be unique")
        operations._add_trusted_delivery_sources(tuple(self._sources.values()))

    def ingest(self, source_id: str, fact: AuthoritativeDeliveryFact) -> AuthoritativeDeliveryFact:
        source = self._sources.get(source_id)
        if source is None:
            raise ConflictError("Trusted delivery source is not configured")
        fact = AuthoritativeDeliveryFact.model_validate(fact.model_dump())
        if fact.source_id != source.id:
            raise ConflictError("Delivery fact provenance does not match the configured source")
        return self._operations._store_authoritative_delivery_fact(fact)


def evaluate_rules(policy: ReturnPolicy, request: EligibilityInput,
                   purchased_quantity: int) -> tuple[str, ...]:
    """Pure rule evaluation. No clock, I/O, or record mutation."""
    reasons = []
    if purchased_quantity == 0:
        reasons.append("item_not_in_order")
    elif request.quantity > purchased_quantity:
        reasons.append("quantity_exceeds_purchased")
    if request.days_since_delivery > policy.return_window_days:
        reasons.append("outside_return_window")
    if request.reason not in policy.allowed_reasons:
        reasons.append("reason_not_allowed")
    if (request.reason == ReturnReason.CHANGE_OF_MIND
            and policy.change_of_mind_requires_unused
            and request.condition != ItemCondition.UNUSED):
        reasons.append("change_of_mind_requires_unused")
    return tuple(reasons)


class BusinessOperations:
    def __init__(self, cases: CaseService,
                 evidence_repository: RefundEvidenceRepository | None = None) -> None:
        self._cases = cases
        self._evidence_repository = evidence_repository
        self._policies = {
            "standard-return": ReturnPolicy(
                id="standard-return", version="demo-v1", return_window_days=30,
                allowed_reasons=tuple(ReturnReason),
                change_of_mind_requires_unused=True, refund_requires_return=True,
                description=("Demo policy, not real merchant terms. Returns within 30 days; "
                             "change of mind requires unused condition. Refund eligibility "
                             "is conditional on a verified return and later authorization."),
            ),
        }
        self._inventory = {
            product.sku: InventoryItem(
                sku=product.sku,
                available_quantity=product.stock_quantity,
                policy_id="standard-return",
            )
            for product in PRODUCT_SEEDS
        }
        self._eligibility_evidence: dict[UUID, EligibilityEvidence] = {}
        self._delivery_facts: dict[tuple[UUID, str], AuthoritativeDeliveryFact] = {}
        self._delivery_fact_ids: dict[UUID, AuthoritativeDeliveryFact] = {}
        self._delivery_source_references: dict[tuple[str, str], AuthoritativeDeliveryFact] = {}
        self._evidence_lock = RLock()

    def _add_trusted_delivery_sources(self,
                                      sources: tuple[TrustedDeliverySource, ...]) -> None:
        """Persist only sources explicitly trusted by host configuration this process."""
        if self._evidence_repository is None:
            return
        for source in sources:
            self._evidence_repository.add_trusted_source(source)

    def get_inventory(self, sku: str) -> InventoryItem:
        if sku not in self._inventory:
            raise NotFoundError("Inventory item not found")
        return self._inventory[sku]

    def get_policy(self, policy_id: str) -> ReturnPolicy:
        if policy_id not in self._policies:
            raise NotFoundError("Policy not found")
        return self._policies[policy_id]

    def has_trusted_delivery_evidence(self, case_id: UUID) -> bool:
        """Return only evidence availability for a case; never expose a fact to a model."""
        case = self._cases.get_case(case_id)
        order = self._cases.get_order(case.order_id)
        for item in order.items:
            if self._evidence_repository is not None:
                fact = self._evidence_repository.get_delivery_fact(case.id, item.sku)
            else:
                with self._evidence_lock:
                    fact = self._delivery_facts.get((case.id, item.sku))
            if fact is not None:
                return True
        return False

    @observed("eligibility")
    def check_eligibility(self, order_id: UUID, request: EligibilityInput,
                          *, case_id: UUID | None = None) -> EligibilityResult:
        self._cases.get_customer(request.customer_id)
        order = self._cases.get_order(order_id)
        if order.customer_id != request.customer_id:
            raise ConflictError("Order does not belong to the selected customer")
        if case_id is not None:
            case = self._cases.get_case(case_id)
            if case.customer_id != request.customer_id or case.order_id != order.id:
                raise ConflictError("Case does not belong to the selected customer and order")
        inventory = self.get_inventory(request.sku)
        policy = self.get_policy(inventory.policy_id)
        purchased = sum(item.quantity for item in order.items if item.sku == request.sku)
        reasons = evaluate_rules(policy, request, purchased)
        result = EligibilityResult(
            case_id=case_id,
            order_id=order.id, customer_id=request.customer_id, sku=request.sku,
            requested_quantity=request.quantity, purchased_quantity=purchased,
            days_since_delivery=request.days_since_delivery,
            reason=request.reason, condition=request.condition,
            policy_id=policy.id, policy_version=policy.version,
            return_eligible=not reasons, refund_eligible=not reasons,
            denial_reasons=reasons, refund_requires_return=policy.refund_requires_return,
        )
        return result

    def _store_authoritative_delivery_fact(self, fact: AuthoritativeDeliveryFact) -> AuthoritativeDeliveryFact:
        """Internal storage called only by DeliveryFactIngestion after provenance validation."""
        fact = AuthoritativeDeliveryFact.model_validate(fact.model_dump())
        case = self._cases.get_case(fact.case_id)
        order = self._cases.get_order(fact.order_id)
        if (case.order_id != order.id or case.customer_id != fact.customer_id
                or order.customer_id != fact.customer_id):
            raise ConflictError("Delivery fact does not match the current case, order, and customer")
        purchased = sum(item.quantity for item in order.items if item.sku == fact.sku)
        if purchased == 0 or fact.quantity > purchased:
            raise ConflictError("Delivery fact does not match an order item")
        if fact.delivered_at.tzinfo is None or fact.verified_at.tzinfo is None:
            raise ConflictError("Delivery fact timestamps must include a timezone")
        if fact.verified_at < fact.delivered_at:
            raise ConflictError("Delivery fact verification cannot predate delivery")
        now = datetime.now(timezone.utc)
        if fact.delivered_at > now or fact.verified_at > now:
            raise ConflictError("Delivery fact timestamps cannot be in the future")
        if self._evidence_repository is not None:
            return self._evidence_repository.store_delivery_fact(fact)
        key = (fact.case_id, fact.sku)
        provenance_key = (fact.source_id, fact.source_reference)
        with self._evidence_lock:
            existing_by_id = self._delivery_fact_ids.get(fact.id)
            if existing_by_id is not None:
                if existing_by_id == fact:
                    return existing_by_id
                raise ConflictError("Delivery fact ID conflicts with an existing record")
            existing_by_provenance = self._delivery_source_references.get(provenance_key)
            if existing_by_provenance is not None:
                if existing_by_provenance == fact:
                    return existing_by_provenance
                raise ConflictError("Delivery fact provenance conflicts with an existing record")
            existing = self._delivery_facts.get(key)
            if existing is not None:
                if existing == fact:
                    return existing
                raise ConflictError("Conflicting delivery fact already exists for this case item")
            self._delivery_facts[key] = fact
            self._delivery_fact_ids[fact.id] = fact
            self._delivery_source_references[provenance_key] = fact
        return fact

    @observed("refund_eligibility")
    def assess_refund_eligibility(self, case_id: UUID, sku: str) -> EligibilityResult:
        case = self._cases.get_case(case_id)
        key = (case.id, sku)
        if self._evidence_repository is not None:
            fact = self._evidence_repository.get_delivery_fact(*key)
        else:
            with self._evidence_lock:
                fact = self._delivery_facts.get(key)
        if fact is None:
            raise ConflictError("Trusted refund evidence unavailable: delivery facts are missing")
        order = self._cases.get_order(case.order_id)
        if (fact.order_id != order.id or fact.customer_id != case.customer_id
                or order.customer_id != case.customer_id):
            raise ConflictError("Trusted refund evidence does not match the current case and order")
        inventory = self.get_inventory(fact.sku)
        policy = self.get_policy(inventory.policy_id)
        now = datetime.now(timezone.utc)
        delivered_at = fact.delivered_at.astimezone(timezone.utc)
        if delivered_at > now:
            raise ConflictError("Trusted refund evidence unavailable: delivery timestamp is in the future")
        days_since_delivery = (now.date() - delivered_at.date()).days
        purchased = sum(item.quantity for item in order.items if item.sku == fact.sku)
        assessment = EligibilityInput(
            customer_id=case.customer_id, sku=fact.sku, quantity=fact.quantity,
            days_since_delivery=days_since_delivery, reason=fact.reason, condition=fact.condition,
        )
        reasons = evaluate_rules(policy, assessment, purchased)
        evidence_id = uuid4()
        result = EligibilityResult(
            evidence_id=evidence_id, case_id=case.id, order_id=order.id,
            customer_id=case.customer_id, sku=fact.sku, requested_quantity=fact.quantity,
            purchased_quantity=purchased, days_since_delivery=days_since_delivery,
            reason=fact.reason, condition=fact.condition, policy_id=policy.id,
            policy_version=policy.version, return_eligible=not reasons,
            refund_eligible=not reasons, denial_reasons=reasons,
            refund_requires_return=policy.refund_requires_return,
            assessment_basis="trusted_delivery_record",
        )
        evidence = EligibilityEvidence(
            id=evidence_id, case_id=case.id, order_id=order.id,
            customer_id=case.customer_id, sku=fact.sku, delivery_fact_id=fact.id,
            delivery_source_id=fact.source_id, delivery_source_reference=fact.source_reference,
            result=result,
            created_at=now,
        )
        if self._evidence_repository is not None:
            self._evidence_repository.store_eligibility_evidence(evidence)
        else:
            with self._evidence_lock:
                self._eligibility_evidence[evidence.id] = evidence
        return result

    def get_eligibility_evidence(self, evidence_id: UUID) -> EligibilityEvidence:
        if self._evidence_repository is not None:
            return self._evidence_repository.get_eligibility_evidence(evidence_id)
        with self._evidence_lock:
            evidence = self._eligibility_evidence.get(evidence_id)
        if evidence is None:
            raise NotFoundError("Eligibility evidence not found")
        return evidence
