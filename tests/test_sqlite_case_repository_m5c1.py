"""Focused M5C1 durable business-record tests; application wiring remains in-memory."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from backend.app.domain import CaseStatus, Customer, Order, OrderItem, ResolutionAction
from backend.app.persistence import PersistenceError, SQLiteCaseRepository, SQLitePersistence
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError, NotFoundError


class SQLiteCaseRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"

    def service(self, *, initialize=False):
        store = SQLitePersistence(self.path, initialize=initialize)
        return CaseService(SQLiteCaseRepository(store))

    @staticmethod
    def order_request(customer_id):
        return OrderCreate(customer_id=customer_id, items=(
            OrderItem(sku="LAP-1", name="Laptop", quantity=1),
            OrderItem(sku="MSE-1", name="Mouse", quantity=2),
        ))

    def test_records_survive_service_recreation_with_relationships(self):
        first = self.service(initialize=True)
        customer = first.create_customer(CustomerCreate(name="Ada Lovelace"))
        order = first.create_order(self.order_request(customer.id))
        case = first.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Wrong item", description="The laptop box contains the wrong model.",
        ))

        recreated = self.service()
        self.assertEqual(recreated.get_customer(customer.id), customer)
        self.assertEqual(recreated.get_order(order.id), order)
        self.assertEqual(recreated.get_case(case.id), case)
        self.assertEqual(recreated.list_orders(customer.id), [order])
        self.assertEqual(recreated.list_cases(customer.id), [case])

    def test_seed_is_idempotent_and_never_overwrites_a_durable_customer(self):
        customer = Customer(id=uuid4(), name="Trusted seed")
        first = self.service(initialize=True)
        first.seed_customer(customer)

        recreated = self.service()
        recreated.seed_customer(customer)
        with self.assertRaises(ValueError):
            recreated.seed_customer(Customer(id=customer.id, name="Replacement seed"))
        self.assertEqual(recreated.get_customer(customer.id), customer)

    def test_invalid_writes_preserve_integrity_and_do_not_partially_persist(self):
        service = self.service(initialize=True)
        customer = service.create_customer(CustomerCreate(name="Owner"))
        other_customer = service.create_customer(CustomerCreate(name="Other owner"))
        order = service.create_order(self.order_request(customer.id))

        with self.assertRaises(NotFoundError):
            service.create_order(self.order_request(uuid4()))
        with self.assertRaises(ConflictError):
            service.create_case(CaseCreate(
                customer_id=other_customer.id, order_id=order.id,
                subject="Not mine", description="This customer does not own that order.",
            ))
        with self.assertRaises(ConflictError):
            service.create_order(OrderCreate(customer_id=customer.id, items=(
                OrderItem(sku="LAP-1", name="Laptop", quantity=1),
                OrderItem(sku="LAP-1", name="Laptop duplicate", quantity=1),
            )))

        self.assertEqual(service.list_orders(customer.id), [order])
        self.assertEqual(service.list_cases(customer.id), [])

    def test_default_case_service_is_still_process_local(self):
        service = CaseService()
        customer = service.create_customer(CustomerCreate(name="In-memory default"))
        self.assertEqual(service.get_customer(customer.id), customer)

    def test_admin_resolution_persists_resolved_status(self):
        service = self.service(initialize=True)
        customer = service.create_customer(CustomerCreate(name="Resolution owner"))
        order = service.create_order(self.order_request(customer.id))
        case = service.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Damaged item", description="The delivered laptop has visible damage.",
        ))
        service.update_status(case.id, CaseStatus.IN_REVIEW)

        resolved, _decision = service.resolve_case(
            case.id, action=ResolutionAction.DECLINE_REPLACEMENT,
            reason="Evidence does not support replacement.",
            customer_message="Please provide additional photos if you would like us to reassess.",
            admin_user_id=uuid4(), evidence_reviewed=False,
        )

        self.assertEqual(resolved.status, CaseStatus.RESOLVED)
        self.assertEqual(self.service().get_case(case.id).status, CaseStatus.RESOLVED)

    def test_resolution_history_is_ordered_and_survives_recreation(self):
        service = self.service(initialize=True)
        customer = service.create_customer(CustomerCreate(name="History owner"))
        order = service.create_order(self.order_request(customer.id))
        case = service.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Damaged item", description="The delivered laptop has visible damage.",
        ))
        requested, first = service.resolve_case(
            case.id, action=ResolutionAction.REQUEST_MORE_EVIDENCE,
            reason="Additional photos are required.",
            customer_message="Please upload clear photos of the damage.",
            admin_user_id=uuid4(), evidence_reviewed=False,
        )
        resolved, second = service.resolve_case(
            case.id, action=ResolutionAction.APPROVE_REPLACEMENT,
            reason="The submitted photos confirm transit damage.",
            customer_message="Your replacement request has been approved.",
            admin_user_id=uuid4(), evidence_reviewed=True,
        )

        reopened = self.service()
        self.assertEqual(requested.status, CaseStatus.IN_REVIEW)
        self.assertEqual(resolved.status, CaseStatus.RESOLVED)
        self.assertEqual(reopened.get_case(case.id), resolved)
        self.assertEqual(reopened.resolution_history(case.id), (first, second))

    def test_decision_insert_failure_rolls_back_status_and_history(self):
        service = self.service(initialize=True)
        customer = service.create_customer(CustomerCreate(name="Rollback owner"))
        order = service.create_order(self.order_request(customer.id))
        case = service.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Damaged item", description="The delivered laptop has visible damage.",
        ))
        service.update_status(case.id, CaseStatus.IN_REVIEW)
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_case_resolution_decision
                   BEFORE INSERT ON case_resolution_decisions
                   BEGIN SELECT RAISE(ABORT, 'forced decision insert failure'); END"""
            )

        with self.assertRaises(PersistenceError):
            service.resolve_case(
                case.id, action=ResolutionAction.DECLINE_REPLACEMENT,
                reason="The evidence does not support replacement.",
                customer_message="Please submit additional evidence for reassessment.",
                admin_user_id=uuid4(), evidence_reviewed=False,
            )

        reopened = self.service()
        self.assertEqual(reopened.get_case(case.id).status, CaseStatus.IN_REVIEW)
        self.assertEqual(reopened.resolution_history(case.id), ())

    def test_duplicate_final_resolution_does_not_create_another_decision(self):
        service = self.service(initialize=True)
        customer = service.create_customer(CustomerCreate(name="Duplicate owner"))
        order = service.create_order(self.order_request(customer.id))
        case = service.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Damaged item", description="The delivered laptop has visible damage.",
        ))
        service.update_status(case.id, CaseStatus.IN_REVIEW)
        _resolved, decision = service.resolve_case(
            case.id, action=ResolutionAction.DECLINE_REPLACEMENT,
            reason="The evidence does not support replacement.",
            customer_message="Please submit additional evidence for reassessment.",
            admin_user_id=uuid4(), evidence_reviewed=False,
        )

        with self.assertRaises(ConflictError):
            service.resolve_case(
                case.id, action=ResolutionAction.DECLINE_REPLACEMENT,
                reason="The evidence does not support replacement.",
                customer_message="Please submit additional evidence for reassessment.",
                admin_user_id=uuid4(), evidence_reviewed=False,
            )
        self.assertEqual(self.service().resolution_history(case.id), (decision,))


if __name__ == "__main__":
    unittest.main()
