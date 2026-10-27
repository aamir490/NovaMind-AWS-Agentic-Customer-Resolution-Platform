"""Focused M5C2 durable trusted-refund evidence tests; runtime remains in-memory."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from backend.app.operations import (
    AuthoritativeDeliveryFact, BusinessOperations, DeliveryFactIngestion,
    TrustedDeliverySource,
)
from backend.app.persistence import (
    PersistenceError, SQLiteCaseRepository, SQLitePersistence,
    SQLiteRefundEvidenceRepository,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError


class SQLiteRefundEvidenceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        self.source = TrustedDeliverySource(id="test-fulfillment")
        self.cases, self.operations, self.ingestion = self.stack(initialize=True)
        self.customer = self.cases.create_customer(CustomerCreate(name="Durable evidence owner"))
        self.order = self.cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))
        self.case = self.cases.create_case(CaseCreate(
            customer_id=self.customer.id, order_id=self.order.id,
            subject="Wrong item", description="Persist trusted refund evidence.",
        ))

    def stack(self, *, initialize=False):
        persistence = SQLitePersistence(self.path, initialize=initialize)
        cases = CaseService(SQLiteCaseRepository(persistence))
        operations = BusinessOperations(cases, SQLiteRefundEvidenceRepository(persistence))
        ingestion = DeliveryFactIngestion(operations, (self.source,))
        return cases, operations, ingestion

    def fact(self, *, delivered_days_ago=10, **changes):
        now = datetime.now(timezone.utc)
        values = {
            "id": uuid4(), "case_id": self.case.id, "order_id": self.order.id,
            "customer_id": self.customer.id, "sku": "LAP-1", "quantity": 1,
            "delivered_at": now - timedelta(days=delivered_days_ago),
            "reason": "wrong_item", "condition": "unused",
            "source_id": self.source.id, "source_reference": "delivery-1",
            "verified_at": now,
        }
        values.update(changes)
        return AuthoritativeDeliveryFact(**values)

    def test_delivery_fact_and_evidence_survive_restart_with_exact_snapshots(self):
        fact = self.ingestion.ingest(self.source.id, self.fact())
        issued = self.operations.assess_refund_eligibility(self.case.id, "LAP-1")
        before = self.operations.get_eligibility_evidence(issued.evidence_id)

        cases, recreated, ingestion = self.stack()
        self.assertEqual(cases.get_case(self.case.id), self.case)
        self.assertEqual(ingestion.ingest(self.source.id, fact), fact)
        self.assertEqual(recreated.get_eligibility_evidence(issued.evidence_id), before)
        self.assertEqual(before.result.policy_id, "standard-return")
        self.assertEqual(before.result.policy_version, "demo-v1")

    def test_immutable_rows_reject_source_provenance_fact_and_policy_changes(self):
        fact = self.ingestion.ingest(self.source.id, self.fact())
        result = self.operations.assess_refund_eligibility(self.case.id, "LAP-1")
        store = SQLitePersistence(self.path)
        mutations = (
            ("UPDATE trusted_delivery_sources SET source_id = ? WHERE source_id = ?",
             ("replacement", self.source.id)),
            ("UPDATE delivery_facts SET source_reference = ? WHERE delivery_fact_id = ?",
             ("forged", str(fact.id))),
            ("UPDATE eligibility_evidence SET policy_version = ? WHERE evidence_id = ?",
             ("forged-v2", str(result.evidence_id))),
        )
        for statement, parameters in mutations:
            with self.subTest(statement=statement), self.assertRaises(PersistenceError):
                with store.transaction() as connection:
                    connection.execute(statement, parameters)

    def test_duplicate_conflicting_and_forged_records_fail_closed(self):
        fact = self.fact()
        self.assertEqual(self.ingestion.ingest(self.source.id, fact), fact)
        self.assertEqual(self.ingestion.ingest(self.source.id, fact), fact)
        with self.assertRaisesRegex(ConflictError, "provenance conflicts"):
            self.ingestion.ingest(self.source.id, self.fact())
        with self.assertRaisesRegex(ConflictError, "Conflicting delivery fact"):
            self.ingestion.ingest(
                self.source.id, self.fact(source_reference="different-reference"),
            )

        result = self.operations.assess_refund_eligibility(self.case.id, "LAP-1")
        evidence = self.operations.get_eligibility_evidence(result.evidence_id)
        forged = evidence.model_copy(update={"delivery_source_reference": "forged"})
        repository = SQLiteRefundEvidenceRepository(SQLitePersistence(self.path))
        with self.assertRaisesRegex(ConflictError, "trusted delivery fact"):
            repository.store_eligibility_evidence(forged)

    def test_database_rejects_cross_bound_evidence_and_rolls_back_transaction(self):
        fact = self.ingestion.ingest(self.source.id, self.fact())
        result = self.operations.assess_refund_eligibility(self.case.id, "LAP-1")
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            snapshot = connection.execute(
                "SELECT assessment_json FROM eligibility_evidence WHERE evidence_id = ?",
                (str(result.evidence_id),),
            ).fetchone()[0]
        forged_id = str(uuid4())
        forged_snapshot = json.dumps({**json.loads(snapshot), "evidence_id": forged_id})
        before = None
        with store.transaction() as connection:
            before = connection.execute("SELECT count(*) FROM eligibility_evidence").fetchone()[0]
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO trusted_delivery_sources(source_id) VALUES (?)", ("rollback-source",),
                )
                connection.execute(
                    """INSERT INTO eligibility_evidence
                       (evidence_id, case_id, order_id, customer_id, sku, delivery_fact_id,
                        policy_id, policy_version, assessment_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (forged_id, str(self.case.id), str(uuid4()), str(self.customer.id), "LAP-1",
                     str(fact.id), "standard-return", "demo-v1", forged_snapshot,
                     datetime.now(timezone.utc).isoformat()),
                )
        with store.transaction() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM trusted_delivery_sources WHERE source_id = 'rollback-source'",
            ).fetchone())
            self.assertEqual(
                connection.execute("SELECT count(*) FROM eligibility_evidence").fetchone()[0], before,
            )

    def test_ineligible_assessment_remains_correct_after_restart(self):
        self.ingestion.ingest(self.source.id, self.fact(delivered_days_ago=31))
        result = self.operations.assess_refund_eligibility(self.case.id, "LAP-1")
        self.assertFalse(result.refund_eligible)
        self.assertEqual(result.denial_reasons, ("outside_return_window",))

        _, recreated, _ = self.stack()
        persisted = recreated.get_eligibility_evidence(result.evidence_id)
        self.assertEqual(persisted.result, result)
        self.assertFalse(persisted.result.authorization_granted)

    def test_default_business_operations_remain_in_memory(self):
        cases = CaseService()
        operations = BusinessOperations(cases)
        self.assertIsNone(operations._evidence_repository)


if __name__ == "__main__":
    unittest.main()
