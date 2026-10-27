import unittest
from uuid import uuid4

from pydantic import ValidationError

from backend.app.domain import CaseStatus, can_transition
from backend.app.main import create_app
from backend.app.schemas import CustomerCreate, OrderCreate, CaseCreate
from backend.app.service import CaseService, ConflictError, NotFoundError


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.service = CaseService()
        self.customer = self.service.create_customer(CustomerCreate(name="  Local customer  "))
        self.order = self.service.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "LAP-1", "name": "Laptop", "quantity": 1}],
        ))

    def request(self, **changes):
        return CaseCreate(**{
            "customer_id": self.customer.id, "order_id": self.order.id,
            "subject": "Wrong laptop", "description": "Received a different model.",
            **changes,
        })

    def test_create_case_relationships_and_server_fields(self):
        case = self.service.create_case(self.request())
        self.assertEqual(self.customer.name, "Local customer")
        self.assertEqual(case.status, CaseStatus.OPEN)
        self.assertEqual(case.created_at, case.updated_at)
        self.assertIsNotNone(case.created_at.tzinfo)
        self.assertEqual(self.service.get_case(case.id), case)
        self.assertEqual(self.service.list_cases(self.customer.id), [case])

    def test_other_customers_order_rejected_without_creating_case(self):
        other = self.service.create_customer(CustomerCreate(name="Other"))
        with self.assertRaises(ConflictError):
            self.service.create_case(self.request(customer_id=other.id))
        self.assertEqual(self.service.list_cases(), [])

    def test_missing_references_rejected(self):
        for changes in ({"customer_id": uuid4()}, {"order_id": uuid4()}):
            with self.subTest(changes=changes), self.assertRaises(NotFoundError):
                self.service.create_case(self.request(**changes))
        with self.assertRaises(NotFoundError):
            self.service.create_order(OrderCreate(customer_id=uuid4(), items=self.order.items))
        with self.assertRaises(NotFoundError):
            self.service.list_cases(uuid4())

    def test_validation_rejects_blank_oversized_and_extra_fields(self):
        for name in ("   ", "x" * 121, None):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                CustomerCreate(name=name)
        for changes in ({"subject": " "}, {"description": "x" * 4001}, {"status": "escalated"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.request(**changes)

    def test_order_item_validation(self):
        for quantity in (0, -1, 1001, True, "1", 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                OrderCreate(customer_id=self.customer.id, items=[{
                    "sku": "LAP-1", "name": "Laptop", "quantity": quantity,
                }])
        with self.assertRaises(ValidationError):
            OrderCreate(customer_id=self.customer.id, items=[])

    def test_complete_transition_matrix(self):
        allowed = {("open", "in_review"), ("open", "escalated"),
                   ("in_review", "escalated"), ("in_review", "resolved")}
        for current in CaseStatus:
            for target in CaseStatus:
                with self.subTest(current=current, target=target):
                    self.assertEqual(can_transition(current, target), current == target or (current, target) in allowed)

    def test_status_update_preserves_identity_and_rejects_reverse(self):
        original = self.service.create_case(self.request())
        updated = self.service.update_status(original.id, CaseStatus.IN_REVIEW)
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertGreaterEqual(updated.updated_at, original.updated_at)
        self.assertEqual(self.service.update_status(updated.id, updated.status), updated)
        with self.assertRaises(ConflictError):
            self.service.update_status(updated.id, CaseStatus.OPEN)
        self.assertEqual(self.service.get_case(updated.id), updated)

    def test_records_are_immutable_and_apps_have_isolated_stores(self):
        with self.assertRaises(ValidationError):
            self.order.items[0].quantity = 99
        first, second = create_app(), create_app()
        customer = first.state.case_service.create_customer(CustomerCreate(name="Isolated"))
        with self.assertRaises(NotFoundError):
            second.state.case_service.get_customer(customer.id)


if __name__ == "__main__":
    unittest.main()
