import unittest
from uuid import uuid4

from pydantic import ValidationError

from backend.app.operations import BusinessOperations, EligibilityInput, evaluate_rules
from backend.app.schemas import CustomerCreate, OrderCreate
from backend.app.service import CaseService, ConflictError, NotFoundError


class BusinessOperationTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseService()
        self.operations = BusinessOperations(self.store)
        self.customer = self.store.create_customer(CustomerCreate(name="Demo"))
        self.order = self.store.create_order(OrderCreate(customer_id=self.customer.id, items=[
            {"sku": "LAP-1", "name": "Laptop", "quantity": 1},
            {"sku": "LAP-1", "name": "Laptop", "quantity": 2},
            {"sku": "LAP-2", "name": "Other laptop", "quantity": 1},
        ]))

    def request(self, **changes):
        return EligibilityInput(**{
            "customer_id": self.customer.id, "sku": "LAP-1", "quantity": 1,
            "days_since_delivery": 10, "reason": "wrong_item", "condition": "unused",
            **changes,
        })

    def check(self, **changes):
        return self.operations.check_eligibility(self.order.id, self.request(**changes))

    def test_window_boundaries(self):
        for days, eligible in ((0, True), (29, True), (30, True), (31, False)):
            with self.subTest(days=days):
                result = self.check(days_since_delivery=days)
                self.assertEqual(result.return_eligible, eligible)
                self.assertEqual(result.refund_eligible, eligible)
                self.assertEqual(result.denial_reasons, () if eligible else ("outside_return_window",))
                self.assertFalse(result.authorization_granted)
                self.assertTrue(result.refund_requires_return)

    def test_reason_condition_matrix(self):
        for reason in ("wrong_item", "damaged", "change_of_mind"):
            for condition in ("unused", "used", "damaged"):
                with self.subTest(reason=reason, condition=condition):
                    self.assertEqual(self.check(reason=reason, condition=condition).return_eligible,
                                     reason != "change_of_mind" or condition == "unused")

    def test_quantity_aggregation_and_all_failure_reasons(self):
        self.assertTrue(self.check(quantity=3).return_eligible)
        result = self.check(quantity=4, days_since_delivery=31,
                            reason="change_of_mind", condition="used")
        self.assertEqual(result.denial_reasons, ("quantity_exceeds_purchased",
                         "outside_return_window", "change_of_mind_requires_unused"))
        self.assertEqual(result.purchased_quantity, 3)

    def test_missing_item_and_disallowed_policy_reason(self):
        policy = self.operations.get_policy("standard-return")
        self.assertEqual(evaluate_rules(policy, self.request(), 0), ("item_not_in_order",))
        restricted = policy.model_copy(update={"allowed_reasons": ()})
        self.assertEqual(evaluate_rules(restricted, self.request(), 1), ("reason_not_allowed",))

    def test_zero_stock_is_known_and_does_not_block_returns(self):
        self.assertEqual(self.operations.get_inventory("LAP-2").available_quantity, 0)
        self.assertTrue(self.check(sku="LAP-2").refund_eligible)
        with self.assertRaises(NotFoundError):
            self.operations.get_inventory("UNKNOWN")
        with self.assertRaises(NotFoundError):
            self.operations.get_policy("UNKNOWN")
        with self.assertRaises(NotFoundError):
            self.check(sku="UNKNOWN")

    def test_missing_customer_order_and_mismatched_owner(self):
        with self.assertRaises(NotFoundError):
            self.check(customer_id=uuid4())
        with self.assertRaises(NotFoundError):
            self.operations.check_eligibility(uuid4(), self.request())
        other = self.store.create_customer(CustomerCreate(name="Other"))
        with self.assertRaises(ConflictError):
            self.check(customer_id=other.id)

    def test_inputs_are_validated(self):
        for change in ({"quantity": 0}, {"quantity": True}, {"days_since_delivery": -1},
                       {"days_since_delivery": 1.5}, {"reason": "other"}, {"sku": " "}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.request(**change)

    def test_repeatability_and_no_mutation(self):
        before_order = self.store.get_order(self.order.id).model_dump()
        before_stock = self.operations.get_inventory("LAP-1").model_dump()
        first, second = self.check(), self.check()
        self.assertEqual(first, second)
        self.assertEqual(self.store.get_order(self.order.id).model_dump(), before_order)
        self.assertEqual(self.operations.get_inventory("LAP-1").model_dump(), before_stock)
        self.assertEqual(self.store.list_cases(), [])
        with self.assertRaises(ValidationError):
            self.operations.get_inventory("LAP-1").available_quantity = 0


if __name__ == "__main__":
    unittest.main()
