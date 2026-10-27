# Phase 5 — Agent-ready local tool layer

Phase 11 extends this original eight-tool catalog with read-only `search_knowledge`; the original tool contracts remain unchanged. See [retrieval contracts and limits](phase-11-knowledge.md).

These are ordinary in-process Python tools, not an agent implementation. `create_app()` wires `app.state.local_tools` to the same local services used by the existing APIs. No SDK, transport, endpoint, dependency, or business rule is added.

## Available tools

- `get_customer`: `customer_id`; returns the customer.
- `get_order`: `order_id`; returns the order and items.
- `get_case`: `case_id`; returns the support case.
- `get_inventory`: `sku`; returns the fixture inventory record.
- `get_policy`: `policy_id`; returns the versioned demonstration policy.
- `assess_eligibility`: `order_id`, `customer_id`, `sku`, `quantity`, `days_since_delivery`, `reason`, `condition`; returns the existing eligibility result, inputs, assumptions, and denial reasons.
- `create_resolution_proposal`: `case_id`, `action`, `rationale`; returns a server-created `PENDING_REVIEW` proposal. Actions remain `return`, `refund`, or `replacement`.
- `get_proposal_status`: `proposal_id`; returns the complete proposal record, including current status and any review metadata.

Inputs use Pydantic models and reject unknown fields. UUIDs, trimmed nonblank strings, enums, and the existing eligibility constraints are validated before dispatch. Business checks remain in existing services; the tool layer does not recalculate eligibility or implement review transitions.

## Calling the interface

In Python, with the existing application instance and a case ID created in that same process:

```python
result = app.state.local_tools.invoke("get_case", {"case_id": str(case_id)})
payload = result.model_dump(mode="json")
descriptions = app.state.local_tools.describe()
```

`describe()` returns fresh descriptions with input JSON schemas, success-data output JSON schemas, and a `read_only` flag. Only proposal creation has `read_only=False`. Output schemas describe the `data` field, not the result envelope.

Success has `ok: true`, `tool`, and validated `data`. Expected failures have `ok: false`, `tool`, and `error` with `code`, `message`, and `issues`. Codes are `UNKNOWN_TOOL`, `INVALID_INPUT`, `NOT_FOUND`, and `CONFLICT`. Validation issues identify fields without including raw input values. An ineligible assessment is a successful tool result with denial reasons, not a transport failure. Unexpected service exceptions or invalid service outputs propagate as defects.

## Boundaries and limitations

The dispatcher uses an explicit allowlist. It offers no approve, reject, case-status update, stock reservation, or action execution tool. Proposal IDs, timestamps, and status come from the existing proposal service. Human review remains separate through Phase 4 APIs; a tool result cannot authorize a refund or replacement.

Proposal creation does not require eligibility or assert entitlement. It records a suggestion for review. Creation is not idempotent: repeating the request creates another pending proposal. Callers must not blindly retry writes.

Eligibility still relies on unverified caller facts and demonstration policies. Reviewer identity remains unverified. This interface is not authentication or a sandbox against arbitrary Python code. Data is process-local, lost on restart, and not shared between application instances/workers. No agent, LLM, external service, persistence, or financial execution is connected. The frontend and existing HTTP contracts are unchanged.

## Focused verification

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_tools.py -v
```

All 12 Phase 5 tests passed using the existing environment. Only these new tests ran; no frontend build, browser check, or full regression was performed.
