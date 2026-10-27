# Phase 1: Application domain foundation

Status: local, in-memory implementation. No AI, external services, authentication, or infrastructure is introduced.

## Domain and boundaries

- A **customer** has a server-generated UUID and a nonblank name (up to 120 characters).
- An **order** belongs to an existing customer and contains 1–100 items. Each item has a nonblank SKU/name (up to 120 characters) and an integer quantity from 1 to 1000. Creating an order records local sample data; it does not place a purchase.
- A **support case/request** links an existing customer to one of that customer's orders. It records a subject (1–200 characters), description (1–4000 characters), status, and server-generated UUID/UTC timestamps. A case is the stored representation of a support request; there is no separate duplicate request entity.

Strings are trimmed before length validation. Unknown body fields are rejected. IDs, initial case status, and timestamps cannot be supplied by the caller. Domain records also serve as validated response models. The relationship check is data consistency, **not authentication or authorization**.

The initial lifecycle is deliberately small:

```text
open -> in_review -> escalated
open -------------> escalated
```

Repeating the current status returns the unchanged record. Backward transitions are rejected. `escalated` is terminal for this phase; it only marks the record, without sending a notification or creating an external escalation. No `resolved` state or action success is claimed because resolution execution/verification does not exist.

`domain.py` contains records and the transition rule; `schemas.py` defines allowed request fields. `service.py` checks relationships/transitions and owns in-memory records behind a lock. `routes.py` is the HTTP boundary; `main.py` creates a fresh service per app instance and translates service failures to HTTP errors. No speculative repository framework has been introduced.

## API surface

- `GET /api/health`: unchanged process-health response.
- `POST /api/customers`: create a local customer; 201.
- `GET /api/customers/{customer_id}`: retrieve a customer; 200.
- `POST /api/orders`: record an order for an existing customer; 201.
- `GET /api/orders/{order_id}`: retrieve an order; 200.
- `POST /api/cases`: create an open case for a matching customer/order; 201.
- `GET /api/cases`: list cases, optionally filtered by `?customer_id=<UUID>`; 200.
- `GET /api/cases/{case_id}`: retrieve a case; 200.
- `PATCH /api/cases/{case_id}/status`: update the status; 200.

Missing records return 404; ownership mismatches or forbidden transitions return 409. Malformed IDs, unknown status values, extra fields, or invalid request bodies return 422. Errors use FastAPI's `detail` envelope. The list is in insertion order; an unknown customer filter returns 404 and a known customer with no cases returns `[]`.

## Local walkthrough (PowerShell)

Start the backend using [local development](local-development.md). Use synthetic data only. Run these commands in another terminal; no AWS credentials or network service is needed:

```powershell
$api = 'http://127.0.0.1:8000/api'
$customer = Invoke-RestMethod -Method Post -Uri "$api/customers" -ContentType 'application/json' -Body (@{name='Demo customer'} | ConvertTo-Json)
$orderBody = @{customer_id=$customer.id; items=@(@{sku='LAP-1'; name='Laptop'; quantity=1})} | ConvertTo-Json -Depth 5
$order = Invoke-RestMethod -Method Post -Uri "$api/orders" -ContentType 'application/json' -Body $orderBody
$caseBody = @{customer_id=$customer.id; order_id=$order.id; subject='Wrong laptop'; description='The received model differs from the ordered model.'} | ConvertTo-Json
$case = Invoke-RestMethod -Method Post -Uri "$api/cases" -ContentType 'application/json' -Body $caseBody
Invoke-RestMethod -Uri "$api/cases/$($case.id)"
Invoke-RestMethod -Method Patch -Uri "$api/cases/$($case.id)/status" -ContentType 'application/json' -Body '{"status":"in_review"}'
Invoke-RestMethod -Uri "$api/cases?customer_id=$($customer.id)"
```

Use `http://127.0.0.1:5173/api` as the base with Vite running to exercise the same endpoints through the existing proxy. The welcome page still only checks health; there is no customer-facing CRUD UI. Interactive local API documentation is available at `http://127.0.0.1:8000/docs`.

## Tests

From the repository root, using the existing environment:

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

Tests cover input validation, missing relationships, mismatched ownership, case transitions, immutable records, isolated app stores, and real HTTP round trips. HTTP tests bind an ephemeral loopback port and stop their server afterward. They use standard-library unittest/urllib plus already installed Uvicorn; no test package is required.

## Limits

All data is process-local, unbounded, and lost on restart/reload. Run a single worker; multiple workers would have separate stores. Lists have no pagination and creation requests have no idempotency key. Use only small local demonstrations. This API has no identity/access control and is not safe to expose as a public service. It has no email/contact validation, prices/payments, attachments, policy eligibility, refunds, replacements, notifications, or durable audit trail. Tests do not establish security, scalability, or production readiness.

Phase 2 requires approval. A suggested next increment is deterministic policy/inventory fixtures and read-only business operations, still local and without AI or AWS.
