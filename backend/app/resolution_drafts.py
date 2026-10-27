"""Grounded, non-executing resolution drafts for supported complaint types."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .domain import Order, Record, SupportCase


class IssueType(StrEnum):
    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    MISSING_ITEM = "missing_item"
    DELAYED_DELIVERY = "delayed_delivery"
    MALFUNCTION = "malfunction"
    RETURN = "return"
    REFUND_NOT_RECEIVED = "refund_not_received"


class RecommendedDecision(StrEnum):
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    ESCALATE = "ESCALATE"


class ResponseSource(StrEnum):
    """Whether customer wording was accepted from Bedrock or rendered locally."""

    BEDROCK_VALIDATED = "BEDROCK_VALIDATED"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


class FallbackReason(StrEnum):
    """Closed reasons for retaining the application-owned deterministic draft."""

    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_OUTPUT_NOT_GROUNDED = "MODEL_OUTPUT_NOT_GROUNDED"


class RefundRecommendationOutcome(StrEnum):
    """A Python-owned assessment outcome; this is never payment authorization."""

    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"


class ResolutionCitation(Record):
    """A compact, application-owned projection of one retrieved policy passage."""

    document_id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    source_uri: str = Field(pattern=r"^local-knowledge://[a-z0-9-]{1,80}$")


class InvestigationOutcome(Record):
    """Sanitized, durable attribution for one Strands investigation result."""

    provider: Literal["bedrock"] = "bedrock"
    model_id: str | None = Field(default=None, max_length=2048,
                                 pattern=r"^[A-Za-z0-9_.:/-]{1,2048}$")
    response_source: ResponseSource
    validation_result: Literal["VALIDATED", "DETERMINISTIC_FALLBACK"]
    fallback_reason: FallbackReason | None = None
    evidence_references: tuple[str, ...] = Field(default=(), max_length=8)
    missing_evidence: tuple[str, ...] = Field(default=(), max_length=6)
    citations: tuple[ResolutionCitation, ...] = Field(default=(), max_length=5)
    model_calls: int = Field(ge=0, le=3)
    tool_calls: int = Field(ge=0, le=32)
    actions_executed: Literal[False] = False

    @model_validator(mode="after")
    def _classification_is_consistent(self):
        if self.response_source is ResponseSource.BEDROCK_VALIDATED:
            if self.validation_result != "VALIDATED" or self.fallback_reason is not None:
                raise ValueError("validated Bedrock output cannot carry fallback metadata")
        elif self.validation_result != "DETERMINISTIC_FALLBACK" or self.fallback_reason is None:
            raise ValueError("deterministic fallback requires a closed fallback reason")
        return self


class RefundRecommendation(Record):
    """Immutable refund assessment projected to staff from trusted inputs only."""

    outcome: RefundRecommendationOutcome
    sku: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    verified_evidence: tuple[str, ...] = Field(default=(), max_length=8)
    missing_evidence: tuple[str, ...] = Field(default=(), max_length=6)
    policy_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    policy_version: str = Field(pattern=r"^[A-Za-z0-9-]{1,160}$")
    decision_reason: str = Field(min_length=1, max_length=1200)
    citations: tuple[ResolutionCitation, ...] = Field(default=(), max_length=5)
    response_source: ResponseSource = ResponseSource.DETERMINISTIC_FALLBACK
    authorization_granted: Literal[False] = False
    actions_executed: Literal[False] = False


class CustomerIntent(StrEnum):
    REPORT_ISSUE = "report_issue"
    TRACK_DELIVERY = "track_delivery"
    REQUEST_RETURN = "request_return"
    REQUEST_REPLACEMENT = "request_replacement"
    REQUEST_REFUND = "request_refund"


InvestigationTool = Literal[
    "get_customer", "get_order", "get_case", "get_inventory", "get_policy", "search_knowledge",
    "assess_eligibility", "assess_refund_eligibility", "create_resolution_proposal", "get_proposal_status",
]


class InvestigationPlan(Record):
    """Application-owned, read-only investigation boundary for one case."""

    issue_type: IssueType
    customer_intent: CustomerIntent
    allowed_tools: tuple[InvestigationTool, ...] = Field(min_length=1, max_length=10)
    required_evidence: tuple[str, ...] = Field(min_length=1, max_length=6)
    max_tool_calls: int = Field(strict=True, ge=1, le=32)
    actions_executed: Literal[False] = False


class ResolutionDraft(Record):
    """Application-rendered draft; this is never an approval or execution."""

    issue_type: IssueType
    recommended_decision: RecommendedDecision
    verified_facts: tuple[str, ...] = Field(min_length=1, max_length=8)
    missing_evidence: tuple[str, ...] = Field(min_length=1, max_length=6)
    decision_reason: str = Field(min_length=1, max_length=1200)
    customer_facing_response: str = Field(min_length=1, max_length=1200)
    troubleshooting_steps: tuple[str, ...] = Field(default=(), max_length=5)
    response_source: ResponseSource = ResponseSource.DETERMINISTIC_FALLBACK
    citations: tuple[ResolutionCitation, ...] = Field(default=(), max_length=5)
    refund_recommendation: RefundRecommendation | None = None
    actions_executed: Literal[False] = False


def build_refund_recommendation(*, sku: str, assessment=None,
                                purchased: bool = True) -> RefundRecommendation:
    """Map server-side assessment evidence to the only three staff-facing outcomes.

    ``assessment`` is an existing trusted ``EligibilityResult``.  Customer or
    model supplied values never reach this function.  Absent/ambiguous facts
    are deliberately evidence requests rather than conditional eligibility.
    """
    if not purchased:
        return RefundRecommendation(
            outcome=RefundRecommendationOutcome.NOT_ELIGIBLE, sku=sku,
            policy_id="standard-return", policy_version="demo-v1",
            missing_evidence=("a purchased item matching the selected SKU",),
            decision_reason="The selected SKU is not present in the case-bound order, so a refund recommendation cannot be supported.",
        )
    if assessment is None:
        return RefundRecommendation(
            outcome=RefundRecommendationOutcome.NEED_MORE_EVIDENCE, sku=sku,
            policy_id="standard-return", policy_version="demo-v1",
            missing_evidence=("trusted delivery and item-condition evidence for the selected purchased SKU",),
            decision_reason="Trusted delivery evidence is unavailable, so refund eligibility cannot be established.",
        )
    evidence = (f"case_id:{assessment.case_id}", f"order_id:{assessment.order_id}",
                f"purchased_sku:{assessment.sku}",
                f"policy:{assessment.policy_id}@{assessment.policy_version}",
                f"eligibility_evidence_id:{assessment.evidence_id}")
    if assessment.refund_eligible and not assessment.denial_reasons and assessment.evidence_id is not None:
        return RefundRecommendation(
            outcome=RefundRecommendationOutcome.ELIGIBLE, sku=str(assessment.sku),
            verified_evidence=evidence, policy_id=str(assessment.policy_id),
            policy_version=str(assessment.policy_version),
            decision_reason="Trusted case-bound delivery evidence satisfies the configured return-policy checks. Staff approval is still required and no refund has been issued.",
        )
    reasons = tuple(sorted(set(assessment.denial_reasons))) or ("refund_eligibility_not_established",)
    return RefundRecommendation(
        outcome=RefundRecommendationOutcome.NOT_ELIGIBLE, sku=str(assessment.sku),
        verified_evidence=evidence, policy_id=str(assessment.policy_id),
        policy_version=str(assessment.policy_version), missing_evidence=(),
        decision_reason="Trusted evidence does not satisfy the configured return-policy checks: " + ", ".join(reasons) + ".",
    )


def attach_refund_recommendation(draft: ResolutionDraft,
                                 recommendation: RefundRecommendation) -> ResolutionDraft:
    """Attach the Python result without treating it as a case decision or message."""
    draft = ResolutionDraft.model_validate(draft)
    recommendation = RefundRecommendation.model_validate(recommendation)
    facts = tuple(dict.fromkeys((*draft.verified_facts, *recommendation.verified_evidence)))[:8]
    missing = recommendation.missing_evidence or draft.missing_evidence
    return draft.model_copy(update={
        "verified_facts": facts,
        "missing_evidence": missing,
        "decision_reason": recommendation.decision_reason,
        "refund_recommendation": recommendation,
    })


def classify_issue_type(*, subject: str, description: str, message: str) -> IssueType:
    """Classify only from case/request text; ambiguous input defaults conservatively."""
    text = " ".join((subject, description, message)).casefold()
    if re.search(r"\b(refund|money|payment).{0,40}\b(not received|missing|hasn.t arrived|never arrived)\b", text):
        return IssueType.REFUND_NOT_RECEIVED
    if re.search(r"\bwrong (item|product|order)|incorrect (item|product)|different item\b", text):
        return IssueType.WRONG_ITEM
    if re.search(r"\bmissing (item|product|part)|item (is )?missing|not in (the )?(box|package)\b", text):
        return IssueType.MISSING_ITEM
    if re.search(r"\b(delayed|late|overdue|not delivered|delivery delay)\b", text):
        return IssueType.DELAYED_DELIVERY
    if re.search(
        r"\b(malfunction|defect|defective|not working|stopped working|broken|"
        r"not functioning(?: properly)?|(?:doesn't|does not|won't|will not) work|"
        r"(?:stops?|is not) responding|unresponsive|intermittent (?:connection|connectivity)|"
        r"fails? to (?:work|start|operate)|"
        r"(?:storage|device|product|item)(?:\s+\w+){0,3}\s+(?:is\s+)?not detected|"
        r"(?:storage|device|product|item)(?:\s+\w+){0,3}\s+(?:cannot|can't) be detected)\b",
        text,
    ):
        return IssueType.MALFUNCTION
    if re.search(r"\b(damaged|damage|cracked|scratched|broken on arrival)\b", text):
        return IssueType.DAMAGED
    if re.search(r"\b(return|send (?:it|this) back|changed my mind)\b", text):
        return IssueType.RETURN
    # There is no separate catch-all issue type.  Route ambiguous technical
    # support wording through the non-fulfillment malfunction path; retain the
    # legacy return fallback for nontechnical ambiguous requests so existing
    # return/HITL workflows remain reachable.
    if re.search(
        r"\b(?:device|input|controller|hardware|software|technical\s+support|"
        r"connection|connectivity)\b",
        text,
    ):
        return IssueType.MALFUNCTION
    return IssueType.RETURN


def build_resolution_draft(case: SupportCase, order: Order, *,
                           trusted_delivery_evidence: bool,
                           policy_id: str, policy_version: str,
                           message: str) -> ResolutionDraft:
    """Create a bounded draft from records and evidence availability only.

    It intentionally does not infer an item, delivery date, payment result, or
    eligibility.  Policy/evidence absence is a reason to request records or
    escalate, never a reason to approve a refund or replacement.
    """
    issue = classify_issue_type(
        subject=case.subject, description=case.description, message=message,
    )
    facts = [f"case_id:{case.id}", f"order_id:{order.id}"]
    if order.items:
        facts.append("ordered_skus:" + ",".join(sorted(item.sku for item in order.items)))
    if issue in (IssueType.RETURN, IssueType.REFUND_NOT_RECEIVED):
        facts.append(f"policy:{policy_id}@{policy_version}")
    if trusted_delivery_evidence:
        facts.append("trusted_delivery_evidence:present")

    if issue is IssueType.REFUND_NOT_RECEIVED:
        decision = RecommendedDecision.ESCALATE
        missing = ("trusted payment/refund settlement record",)
        reason = "Payment or refund settlement evidence is not available in the verified case records."
    elif issue is IssueType.MALFUNCTION:
        # Delivery evidence cannot establish a technical diagnosis.  Request
        # only diagnostic details the customer can safely provide; do not infer
        # a fault, warranty outcome, or remedy from the reported symptom.
        decision = RecommendedDecision.REQUEST_EVIDENCE
        missing = (
            "reported symptom, when it occurs, and steps already tried",
            "product and connection/setup details, including any visible error indicator",
        )
        reason = (
            "The report describes a technical issue, but the verified records do not establish "
            "a diagnosis. Diagnostic details are needed for support review."
        )
    elif issue is IssueType.DELAYED_DELIVERY:
        decision = RecommendedDecision.ESCALATE
        missing = ("trusted shipment tracking status", "committed delivery date")
        reason = "Delivery timing cannot be established from the available verified records."
    elif issue is IssueType.RETURN:
        decision = RecommendedDecision.REQUEST_EVIDENCE
        missing = (("affected item and condition", "return or delivery evidence")
                   if trusted_delivery_evidence else
                   ("affected item and condition", "trusted shipping or delivery evidence"))
        reason = "The policy is available, but required item and condition evidence is not verified."
    elif not trusted_delivery_evidence:
        decision = RecommendedDecision.REQUEST_EVIDENCE
        missing = ("trusted shipping or delivery evidence", "case-specific supporting evidence")
        reason = "The complaint is recorded, but shipment/delivery facts have not been verified."
    else:
        decision = RecommendedDecision.ESCALATE
        missing = ("staff review of the case-specific supporting evidence",)
        reason = "A delivery record exists, but the issue still requires a staff decision under policy."

    if issue is IssueType.MALFUNCTION:
        response = (
            "We have recorded the technical issue. Please tell us when the symptom occurs, "
            "the steps you have already tried, your product and connection/setup details, and "
            "any visible error indicator. We will use those diagnostic details for support review. "
            "No refund, replacement, return, or payment action has been executed."
        )
    else:
        response = (
            "We have recorded your request. "
            + ("We need the listed evidence before eligibility can be assessed. "
               if decision is RecommendedDecision.REQUEST_EVIDENCE
               else "A staff member must review the available records and missing evidence. ")
            + "No refund, replacement, return, or payment action has been executed."
        )
    return ResolutionDraft(
        issue_type=issue, recommended_decision=decision, verified_facts=tuple(facts),
        missing_evidence=missing, decision_reason=reason,
        customer_facing_response=response,
    )


def build_investigation_plan(draft: ResolutionDraft, *, request_text: str,
                             refund_requested: bool, max_tool_calls: int) -> InvestigationPlan:
    """Return a small issue-specific allowlist without authorizing any action.

    Refund intent is trusted only when the request schema supplied it explicitly;
    customer prose alone cannot put the refund assessment tool in an LLM plan.
    """
    issue = draft.issue_type
    if refund_requested:
        intent = CustomerIntent.REQUEST_REFUND
        tools: tuple[InvestigationTool, ...] = (
            "get_case", "get_order", "assess_refund_eligibility", "create_resolution_proposal", "get_proposal_status",
        )
    elif issue is IssueType.DELAYED_DELIVERY:
        intent = CustomerIntent.TRACK_DELIVERY
        tools = ("get_case", "get_order", "search_knowledge", "create_resolution_proposal", "get_proposal_status")
    elif issue is IssueType.REFUND_NOT_RECEIVED:
        intent = CustomerIntent.REPORT_ISSUE
        tools = ("get_case", "get_order", "search_knowledge", "create_resolution_proposal", "get_proposal_status")
    elif issue is IssueType.RETURN:
        intent = CustomerIntent.REQUEST_RETURN
        tools = ("get_case", "get_order", "get_policy", "search_knowledge", "create_resolution_proposal", "get_proposal_status")
    elif issue in (IssueType.DAMAGED, IssueType.WRONG_ITEM):
        intent = CustomerIntent.REQUEST_REPLACEMENT if re.search(
            r"\b(replace|replacement|exchange)\b", request_text, re.IGNORECASE,
        ) else CustomerIntent.REPORT_ISSUE
        tools = (
            "get_customer", "get_case", "get_order", "get_inventory", "get_policy", "assess_eligibility",
            "search_knowledge", "create_resolution_proposal", "get_proposal_status",
        )
    elif issue is IssueType.MISSING_ITEM:
        # This issue type has no authoritative ReturnReason.  Do not coerce it
        # into another policy reason; collect evidence and escalate instead.
        intent = CustomerIntent.REQUEST_REPLACEMENT if re.search(
            r"\b(replace|replacement|exchange)\b", request_text, re.IGNORECASE,
        ) else CustomerIntent.REPORT_ISSUE
        tools = (
            "get_customer", "get_case", "get_order", "get_inventory", "get_policy",
            "search_knowledge", "create_resolution_proposal", "get_proposal_status",
        )
    elif issue is IssueType.MALFUNCTION:
        # Technical support may provide troubleshooting, request evidence, or
        # escalate.  It never authorizes a fulfillment/financial proposal.
        intent = CustomerIntent.REPORT_ISSUE
        tools = (
            "get_customer", "get_case", "get_order", "get_inventory",
            "search_knowledge",
        )
    else:  # Defensive fallback if a future issue type is introduced without a plan.
        intent = CustomerIntent.REPORT_ISSUE
        tools = ("get_case", "get_order", "search_knowledge", "create_resolution_proposal", "get_proposal_status")
    return InvestigationPlan(
        issue_type=issue, customer_intent=intent, allowed_tools=tools,
        required_evidence=draft.missing_evidence, max_tool_calls=max_tool_calls,
    )
