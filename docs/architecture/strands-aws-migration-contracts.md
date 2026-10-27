# Strands-only AWS migration contracts

Status: **TASK 1 DESIGN / NOT IMPLEMENTED**

This document defines the contract boundary for the approved Strands-only AWS
migration. It does not change the runtime architecture. The final runtime is
AWS-only: Strands plus Amazon Bedrock performs bounded investigation, FastAPI
services perform deterministic policy/proposal/HITL transitions, PostgreSQL is
the system of record, and SQS delivers work to an ECS worker.

The final runtime contains no LangGraph, SQLite, local scripted provider, local
authentication provider, in-process investigation dispatcher, or local Compose
deployment path.

## Scope and non-goals

This task specifies contracts only. It does not implement the migration, add
dependencies, change API handlers, alter database migrations, add AWS
resources, or remove current compatibility code.

The migration remains proposal-only. Approval records a human decision; it does
not issue a refund, create a replacement, mutate inventory, or send a customer
communication unless a separately approved execution capability is designed.

## Durable identifiers

Every identifier below is generated or validated by the application. No model
output, customer field, or browser-generated role value is trusted as authority.

| Identifier | Owner | Purpose | Idempotency/uniqueness boundary |
| --- | --- | --- | --- |
| `customer_id` | PostgreSQL customer record | Customer ownership and projections | Stable customer primary key |
| `order_id` | PostgreSQL order record | Order/customer relationship | Stable order primary key |
| `case_id` | PostgreSQL support case | End-to-end customer/admin subject | One case is the lifecycle anchor |
| `job_id` | PostgreSQL investigation job | Queue payload and worker claim target | One active job per case/intake request |
| `workflow_id` | PostgreSQL workflow metadata | Durable investigation execution identity | Bound to one job and case |
| `run_id` | API/workflow projection | Frontend status and event identity | Stable across API/worker replacement |
| `request_id` | API caller contract | Safe retry of a requested investigation | Unique per authenticated owner/request |
| `proposal_id` | PostgreSQL proposal | Proposed resolution identity | One proposal per workflow review binding |
| `review_id` | PostgreSQL review binding | Exact HITL decision target | One review anchor per proposal/workflow |
| `claim_id` | PostgreSQL worker claim | Lease-fencing token | Replaced only by an explicit recovery transition |
| `attempt_id` | PostgreSQL execution attempt | One durable worker execution reservation | At most one committed attempt per job |
| `outbox_id` | PostgreSQL outbox row | Queue publication identity | Unique message publication record |
| `idempotency_key` | HTTP request header | Retry-safe write command | Unique within command/owner scope |
| `trace_id` | HTTP/worker observability | Correlates a request and its events | Diagnostic only; never authorization |

`run_id` and `workflow_id` must not depend on API process memory or an
`instance_id`. Existing `instance_id` and process-scoped response fields remain
compatibility concerns until the API contract migration is implemented.

## Strands + Bedrock investigation contract

The existing implementation is in
`backend/app/strands_agent.py`, primarily:

- `READ_ONLY_TOOLS`
- `_ToolCallBudget.consume`
- `_investigation_instructions`
- `StrandsWorkflow.start`
- `StrandsWorkflow._make_strands_tools`
- `StrandsWorkflow._wrap_tool`
- `StrandsWorkflow._fail`

The final contract retains the bounded tool allowlist and replaces the current
in-memory workflow result with a durable, structured investigation result.

### Input

The worker loads the input from PostgreSQL using the fenced `job_id`; it does
not accept the complete request from SQS. The validated investigation input is
derived from:

- `case_id`
- `order_id`
- customer ownership already verified by FastAPI
- `workflow_id`
- `attempt_id`
- the immutable request fingerprint currently produced by
  `HITLWorkflow._fingerprint` and related job repository logic
- bounded customer text and issue category
- approved policy/evidence query parameters

SQS contains a bounded reference such as `job_id`, `outbox_id`, and a message
schema/version. It must not contain bearer tokens, database credentials,
unbounded prompts, or authoritative policy decisions.

### Model-controlled tools

The only model-visible tool bindings may be the audited read-only set currently
defined by `READ_ONLY_TOOLS`:

- `search_knowledge`
- `get_customer`
- `get_order`
- `get_case`
- `get_inventory`
- `get_policy`
- `assess_eligibility`
- `get_proposal_status`

The final implementation must re-audit each binding through
`LocalTools.invoke`. A binding is not read-only merely because its metadata
says so. In particular, `BusinessOperations.assess_refund_eligibility` may
persist evidence and therefore cannot silently become a model-authorized
mutation.

Strands must never receive tools for:

- approval or rejection;
- proposal review;
- inventory mutation;
- refund or replacement execution;
- customer communication;
- identity or role assignment;
- queue acknowledgement;
- database migration or arbitrary SQL.

### Output

Strands returns a bounded structured investigation result containing:

- `case_id`
- `workflow_id`
- `attempt_id`
- investigation status;
- observed facts and source references;
- missing evidence, if any;
- a recommendation or `NEEDS_REVIEW` outcome;
- bounded explanation text;
- policy/evidence references;
- provider metadata and safe failure category;
- no authorization decision;
- no execution command.

The deterministic services validate this result before any proposal is created.
Model wording cannot change eligibility, actor identity, proposal status, or
case status.

### Failure contract

Failures use closed categories already represented by the current provider and
diagnostic contracts, such as timeout, unavailable provider, invalid response,
step limit, unsafe content, and workflow error. Raw exception text, prompts,
credentials, and arbitrary provider payloads are not persisted to customer or
admin API responses.

## PostgreSQL-owned state transitions

The current PostgreSQL composition is in
`backend/app/postgres_persistence.py` and is selected by
`backend/app/main.py:create_app`. The final contract makes PostgreSQL the
authoritative store for all transitions below.

### Case and intake

`backend/app/customer_case_routes.py:create_my_case` derives the customer from
Cognito identity and validates order ownership. The final transition is:

```text
customer request
  -> create support case
  -> create one durable investigation job
  -> create transactional outbox row
  -> commit
  -> publish job reference to SQS
```

The case and job write must be in one PostgreSQL transaction. A retry with the
same `idempotency_key` returns the original case/job result and does not create
a second job or outbox row.

### Job and worker state

The existing durable concepts come from
`SQLiteInvestigationJobRepository` in `backend/app/persistence.py` and the
PostgreSQL subclass in `backend/app/postgres_persistence.py`:

- `submit`
- `register_manual_start`
- `claim`
- `claim_next`
- `bind_claim`
- `mark_execution_attempt`
- `execution_context`
- `execution_request`
- `require_recovery`

The final PostgreSQL-native implementation must preserve these semantics while
removing production dependence on SQLite inheritance.

Required job states:

```text
PENDING
  -> CLAIMED
  -> RUNNING
  -> REVIEW_REQUIRED
  -> RESUME_REQUESTED
  -> COMPLETED
```

Failure paths are explicit:

```text
PENDING/RUNNING -> RECOVERY_REQUIRED
PENDING/RUNNING -> FAILED
malformed/repeated delivery -> DLQ_REVIEW_REQUIRED
```

Expired leases do not automatically mean safe retry. Recovery must be an
explicit, audited transition that creates a new fenced claim or a new attempt
according to policy.

### Proposal and HITL state

The deterministic proposal boundary is currently represented by
`backend/app/proposals.py:ProposalService` and
`backend/app/proposals.py:ProposalRepository`.

Required sequence:

```text
validated Strands result
  -> deterministic policy/evidence recheck
  -> create PENDING_REVIEW proposal
  -> bind proposal to workflow_id/case_id/review_id
  -> persist REVIEW_REQUIRED workflow/job state
  -> emit durable review event
```

Admin decision requires all of:

- Cognito-authenticated `REVIEWER` or `ADMIN` identity;
- matching `case_id`;
- matching `workflow_id`;
- matching `proposal_id`;
- matching `review_id`;
- proposal still `PENDING_REVIEW`;
- case still eligible for the requested decision;
- idempotency key not previously consumed by a conflicting decision.

The decision transition is atomic:

```text
PENDING_REVIEW
  -> APPROVED or REJECTED
  -> workflow/job state updated
  -> audit event written
  -> optional durable resume/outbox command written
```

Approval is not execution. `actions_executed` remains `false` in all current
contracts.

## Transactional outbox contract

The final API must not commit PostgreSQL state and then rely on an unrelated
best-effort SQS call. Every queue-producing state transition writes an outbox
row in the same PostgreSQL transaction as the business change.

The outbox record must contain at least:

- `outbox_id`
- message type/version;
- aggregate type and aggregate ID;
- `job_id` or `workflow_id`;
- deduplication key;
- serialized bounded message body or a reference to the durable record;
- created timestamp;
- publication status;
- publication attempt count;
- last error category;
- published timestamp.

The outbox body must contain identifiers and versioned intent only. It must not
contain secrets, bearer tokens, unrestricted customer text, or a model-generated
authority claim.

The publisher/worker contract is:

1. PostgreSQL transaction writes business state and outbox row.
2. A publisher reads unpublished rows with row-level locking.
3. The publisher sends the bounded SQS message.
4. The publisher marks the outbox row published using its `outbox_id`.
5. Duplicate publication is safe because the consumer rechecks PostgreSQL
   state and claim fencing before execution.
6. A failed send leaves the outbox row retryable and observable.

The outbox publisher must not mark a message published before SQS confirms the
send. SQS delivery remains at-least-once; PostgreSQL idempotency remains the
source of truth.

## Lease-fencing contract

Every worker mutation after claim must include and validate:

- `job_id`
- `claim_id`
- `worker_id`
- lease ownership and expiry;
- `attempt_id`
- expected current job/workflow state.

Every update must be conditional on the active fence. A stale worker must get a
closed conflict/recovery result and must not:

- create a proposal;
- append a terminal event;
- record an admin decision;
- mark a job completed;
- delete or acknowledge a newer SQS delivery.

Long-running Bedrock/Strands work must renew the lease or fail into
`RECOVERY_REQUIRED`. Lease renewal must itself be fenced. A worker that loses
the lease must stop using the model result even if the provider call later
returns.

## API compatibility

Existing route families should remain stable during migration:

- `/api/my/orders`
- `/api/my/cases`
- `/api/cases/{case_id}`
- `/api/cases/{case_id}/investigation`
- `/api/runs`
- `/api/runs/{run_id}`
- `/api/runs/{run_id}/events`
- `/api/reviews`
- `/api/runs/{run_id}/decision`
- `/api/cases/{case_id}/lifecycle`
- `/api/admin/diagnostics/*`

The current route implementations are in:

- `backend/app/customer_order_routes.py`
- `backend/app/customer_case_routes.py`
- `backend/app/routes.py`
- `backend/app/frontend_routes.py`
- `backend/app/proposal_routes.py`

Compatibility rules:

- preserve request field names where they are not process-specific;
- preserve `Idempotency-Key` requirements for writes;
- preserve HTTP `202` for asynchronous investigation submission;
- preserve review binding fields and conflict behavior;
- preserve customer-safe lifecycle messages;
- remove process authority from `instance_id`;
- replace process-scoped values such as
  `pending_reviews_in_this_process` with durable scope values;
- remove LangGraph node names from public event contracts;
- make `run_id`, `workflow_id`, `job_id`, and `review_id` durable across API
  replacement;
- never expose raw Strands prompts, tool payloads, or provider exceptions.

Affected schemas include:

- `backend/app/frontend_contracts.py:RunStart`
- `RunSnapshot`
- `RunEvent`
- `RunPage`
- `PendingReviewPage`
- `AuditPage`
- `CustomerCaseLifecycle`
- `DurableInvestigationItem`
- `backend/app/domain.py:InvestigationJob`
- `backend/app/workflow_metadata.py` status contracts

The frontend validators and projections requiring compatibility updates are in:

- `frontend/src/api.js`
- `frontend/src/CaseReview.jsx`
- `frontend/src/ReviewQueue.jsx`
- `frontend/src/ReviewRunResult.jsx`
- `frontend/src/MySupportRequests.jsx`
- `frontend/src/auditContracts.js`
- `frontend/src/diagnosticsContracts.js`

## Idempotency rules

The following operations must be idempotent:

- customer order creation;
- customer case creation;
- manual investigation start;
- automatic investigation submission;
- outbox publication;
- worker claim/attempt reservation;
- proposal creation;
- HITL approval/rejection;
- recovery command creation.

An identical retry returns the previously committed result. A retry with the
same idempotency key but a different canonical request fingerprint returns a
conflict. The fingerprint must include the case, requested intent, relevant
SKU/evidence selectors, authenticated owner, and contract version.

No idempotency implementation may depend on an API process-local dictionary.

## Recovery and uncertain outcomes

The final API must distinguish these states:

- `PENDING`: accepted but not claimed;
- `CLAIMED`: owned by a worker lease;
- `RUNNING`: Strands/Bedrock investigation active;
- `REVIEW_REQUIRED`: proposal and review binding durably committed;
- `RESUME_REQUESTED`: human decision committed and worker continuation queued;
- `COMPLETED`: terminal deterministic state committed;
- `FAILED`: safe terminal failure with no execution;
- `RECOVERY_REQUIRED`: ambiguous or fenced execution requiring explicit review;
- `DLQ_REVIEW_REQUIRED`: delivery/serialization failure requiring operator action.

The frontend must never infer success from an accepted HTTP response. It must
read the durable state and display an uncertain/recovery state when the result
cannot be confirmed.

## Acceptance tests for Task 1

Task 1 is accepted when the following test plan is documented and later
implemented before runtime migration:

### Existing tests to preserve or adapt

- `tests/test_strands_agent.py`
  - read-only allowlist;
  - tool budget;
  - safe provider failure;
  - structured result validation;
  - no authority-bearing tool.

- `tests/test_bedrock.py`
- `tests/test_bedrock_provider_regression.py`
- `tests/test_llm_diagnostics.py`
- `tests/test_security.py`
- `tests/test_cognito_authentication_phase_b.py`
- `tests/test_phase17b3_intake.py`
- `tests/test_customer_case_lifecycle_endpoint.py`
- `tests/test_customer_communications.py`
- `tests/test_proposals.py`
- `tests/test_proposal_security_m1.py`
- `tests/test_refund_orchestration_m3a.py`
- `tests/test_resolution_drafts_bi.py`
- `tests/test_frontend_contracts.py`
- `tests/test_postgres_persistence_phase_a.py`
- `tests/test_postgres_integration_phase_c.py`

### Required new acceptance cases

1. Case creation, job creation, and outbox insertion commit atomically.
2. A transaction rollback creates neither a visible job nor an outbox row.
3. Repeating the same intake `Idempotency-Key` returns the original IDs.
4. Reusing an idempotency key with a changed fingerprint returns conflict.
5. Two workers competing for one job produce one claim and one attempt.
6. A stale `claim_id` cannot update job, workflow, proposal, or audit state.
7. Lease renewal is fenced and expiry enters `RECOVERY_REQUIRED` safely.
8. Duplicate SQS delivery cannot create a second proposal.
9. Outbox send failure leaves a retryable unpublished row.
10. Outbox publication duplication remains consumer-idempotent.
11. Strands output cannot change deterministic policy eligibility.
12. Strands cannot approve, reject, execute, or mutate inventory.
13. A proposal is bound to the exact case/workflow/review IDs.
14. Duplicate approval/rejection is rejected without a second transition.
15. HITL state survives API and worker process replacement.
16. Customer lifecycle projections remain safe after worker failure.
17. Public API responses contain no raw prompts, credentials, or provider errors.
18. Review queue and run events are durable across multiple API instances.

## Remaining design risks

- The current PostgreSQL repositories inherit from SQLite repositories. This
  must be removed or fully isolated before production approval.
- The existing PostgreSQL migration list is substantial and requires a
  compatibility check before any schema changes are introduced.
- The current `StrandsWorkflow` keeps run state in memory and has no durable
  resume/audit implementation.
- The current `FrontendRuntime` is process-oriented and cannot be the final
  multi-task API state model.
- No SQS, outbox, DLQ, worker task, or worker IAM implementation currently
  exists.
- Existing frontend contracts explicitly contain process-scoped values.
- At-least-once SQS delivery and long Bedrock calls require careful lease and
  visibility-timeout coordination.
- Approval remains proposal-only; execution must not be implied by this
  migration.
