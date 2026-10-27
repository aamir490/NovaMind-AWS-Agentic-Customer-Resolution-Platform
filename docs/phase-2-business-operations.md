# Phase 2: Deterministic business operations

Local demonstration only. Policy terms below are invented fixtures, not legal guidance or a real merchant's policy. No agents, external services, authentication, database, payment/refund execution, or infrastructure is introduced.

## Read-only endpoints

- `GET /api/inventory/{sku}`: local available quantity and associated policy ID.
- `GET /api/policies/{policy_id}`: versioned return/refund demonstration policy.
- `GET /api/orders/{order_id}/eligibility`: conditional return/refund eligibility and explicit denial reasons.
- Existing `GET /api/customers/{customer_id}` and `GET /api/orders/{order_id}` provide customer/order lookup. These reuse the Phase 1 store; there is no duplicate customer/order fixture system.

Eligibility requires query fields `customer_id`, `sku`, `days_since_delivery`, `reason`, and `condition`; `quantity` defaults to 1. IDs must be UUIDs, quantity is 1–1000, and elapsed days is 0–36500. SKU matching is case-sensitive. Customer/order records must first exist through the Phase 1 local setup endpoints. Those existing write endpoints remain available; all newly added Phase 2 endpoints are GET-only.

## Fixtures and explicit rules

`LAP-1` has 5 available units. `LAP-2` has 0 available units. Both reference `standard-return`, version `demo-v1`. Unknown stock is a 404, not assumed zero stock. Unknown policies, customers, or orders also return 404. Ownership mismatches return 409; malformed inputs return 422.

The fixture policy allows `wrong_item`, `damaged`, and `change_of_mind` reasons. Valid conditions are `unused`, `used`, and `damaged`.

1. The selected customer must own the order. This is relationship consistency, not authenticated ownership.
2. The selected SKU must occur in the order. Quantities across repeated SKU lines are summed.
3. Requested quantity must not exceed the purchased quantity.
4. Days since delivery must be within 0–30 inclusive. Day 30 passes; day 31 fails.
5. The reason must be allowed by the policy.
6. `change_of_mind` requires `unused` condition. Wrong-item and damaged claims allow any listed condition under this demonstration policy.

All applicable denial reasons are returned in stable order: `item_not_in_order` (or `quantity_exceeds_purchased`), `outside_return_window`, `reason_not_allowed`, and `change_of_mind_requires_unused`. A known SKU absent from the order produces an ineligible result, not a missing-order error. A valid assessment returns 200 even when ineligible.

`return_eligible` and `refund_eligible` both mean the sample policy's prerequisites are met under the supplied facts. A refund remains conditional on a verified return and later authorization: `refund_requires_return` is true, and `authorization_granted` is always false. Stock is informational and does not restrict return/refund eligibility. There is no replacement eligibility or reservation operation.

## Facts versus assumptions

Phase 1 orders have no delivery date, payment history, verified item condition, or prior-return ledger. Elapsed days, reason, and condition are caller-supplied scenario inputs. Results explicitly carry `assessment_basis: local_scenario_unverified_inputs` and echo the evaluated facts and policy version. They do not establish actual entitlement, a refund amount, or permission to execute an action.

The evaluator has no wall-clock dependency or I/O. Repeating identical inputs against identical records/fixtures returns identical results. Checks do not change orders, case status/timestamps, inventory, or policy. Returned fixture models are immutable and fixtures reset with the application.

## Local example

Create a customer and an order with SKU `LAP-1` using the [Phase 1 walkthrough](phase-1-domain.md). With its `$customer` and `$order` variables:

```powershell
$api = 'http://127.0.0.1:8000/api'
Invoke-RestMethod "$api/inventory/LAP-1"
Invoke-RestMethod "$api/policies/standard-return"
Invoke-RestMethod "$api/orders/$($order.id)/eligibility?customer_id=$($customer.id)&sku=LAP-1&quantity=1&days_since_delivery=30&reason=wrong_item&condition=unused"
```

Change elapsed days to 31 for an ineligible result. With Vite running, base URL `http://127.0.0.1:5173/api` exercises the same operations through the existing proxy. The UI remains a welcome/health page, not a business-operations interface.

## Tests and limitations

Run `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -v` from the repository root. Tests cover boundary days, all reason/condition combinations, summed quantities, unknown records, invalid requests, ownership mismatch, denied assessments, read-only HTTP methods, repeatability, and unchanged case/inventory data. No extra test package is needed.

Existing limits remain: memory-only data, one worker, no authentication, no persistent audit, no prior-return accounting, no evidence verification, and no real merchant/payment integration. Never treat repeated eligible assessments as permission for repeated refunds. Use synthetic data on loopback only.

Suggested Phase 3, subject to approval: connect a minimal local case-review UI to these operations, exposing assumptions and denial reasons. No Phase 3 work is part of this increment.
