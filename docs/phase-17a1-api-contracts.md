# Phase 17A.1 — minimal frontend API adapters

This implements the initial backend-contract slice of the Phase 17A roadmap, as
clarified for this task. UI, frontend authentication storage, SSE, AWS, and Phase 18
are outside this change. Existing case/context/proposal reads remain the dashboard
source; no aggregate metrics or synthetic activity were added.

## Trusted local composition

`create_app(auth_provider=..., local_provider_factory=..., conversation_store=...)`
composes the existing `LocalTools`, `StructuredLLM`, `HITLWorkflow`, and
`ConversationService` over the same case/proposal stores. The optional factory must
return a fresh **local scripted** provider per run; this adapter labels it
`local_scripted`. It is a trusted Python configuration seam, never a browser field
or a live-provider configuration endpoint. The tests exercise this seam through
real ASGI requests, LangGraph nodes, tools, proposals, and temporary SQLite.

Defaults remain deny/unavailable: an empty credential registry, no provider, and
no conversation store. Provider absence returns `503 PROVIDER_UNAVAILABLE`;
history absence returns `503 CONVERSATION_UNAVAILABLE`. The container bootstrap
does not provision either component or change networking. Explicitly supply an
existing Phase 12 `SQLiteConversationStore` to enable conversation history. No
database is created by HTTP startup.

## HTTP contracts

All new endpoints require the existing `Authorization: Bearer ...` header. API
responses include a server-generated `X-Trace-ID` and `Cache-Control: no-store`.
Body models reject extra fields; validation responses do not echo rejected input.
The exact schemas are defined in `backend/app/frontend_contracts.py` and OpenAPI.

- `GET /api/identity`: authenticated identity, review/diagnostic permissions,
  process `instance_id`, provider availability/label, and memory availability.
- `POST /api/runs`: accepts `instance_id`, client-generated UUID `request_id`,
  `case_id`, a nonblank message of at most 8,000 characters, and optional
  `conversation_id`. Returns `202` with a server-generated `run_id` and snapshot.
  This is also the user-message submission path: the existing workflow records
  user text and application-generated assistant outputs when memory is enabled.
  There is no client assistant-message writer or generic tool-execution endpoint.
- `GET /api/runs/{run_id}`: current snapshot, including workflow/conversation IDs
  as they become available, status, safe application message/error, original review
  binding, reviewed status, sequence, timestamps, and trace correlation.
- `GET /api/runs/{run_id}/events?after=0&limit=50`: ordered incremental events and
  an atomic snapshot. Returns `next_after`, `has_more`,
  `first_available_sequence`, `dropped_events`, and `gap`.
- `GET /api/cases/{case_id}/runs?offset=0&limit=50`: authorized, bounded summaries
  of runs retained in this process, in creation order, with `next_offset`.
- `GET /api/cases/{case_id}/conversations?offset=0&limit=50`: existing saved
  conversation metadata with `next_offset`.
- `GET /api/cases/{case_id}/conversations/{conversation_id}?after=0&limit=50`:
  saved messages in sequence order with `next_after` and `has_more`. Conversation
  text is explicitly untrusted and cannot restore domain/workflow state.
- `POST /api/runs/{run_id}/decision`: REVIEWER/ADMIN only; accepts `instance_id`,
  all four workflow/case/proposal/review IDs, `APPROVE` or `REJECT`, and optional
  note. Returns `202 RESUMING`; poll for the outcome. Calls only
  `HITLWorkflow.resume`, which derives reviewer identity from authentication.
- `GET /api/runs/{run_id}/audit?after=0&limit=50`: REVIEWER/ADMIN only, sanitized
  workflow audit metadata. During execution returns `409 AUDIT_PENDING_USE_EVENTS`
  so it cannot block live polling on the workflow lock.
- `GET /api/diagnostics?limit=50`: REVIEWER/ADMIN only, the existing observer's
  bounded metadata and process metrics, snapshot timestamp, evicted/omitted event
  counts, and logging failures. Unknown token counts remain unknown rather than
  being presented as zero usage.

Page sizes are 1–100. CUSTOMER starts and reads check current case ownership;
CUSTOMER run reads additionally require the initiating user. Unknown and foreign
customer run IDs both return 403. REVIEWER/ADMIN may inspect authorized runs and
diagnostics. Conversation reads retain the existing case-ownership rules.

## Execution, reconnect, and review boundaries

Execution uses at most two worker threads with no pending admission queue. Up to
128 runs/checkpoints/deduplication keys are retained for the process lifetime;
capacity exhaustion returns 503. Runs are not evicted and their request IDs do
not become reusable. Each run retains its latest 128 events. These defaults are
trusted runtime settings, never request parameters.

The same user/request ID and identical body return the original run without a new
provider call, proposal, or memory append. A changed body returns
`409 REQUEST_ID_CONFLICT`. A stale process ID returns
`409 INSTANCE_CHANGED_DO_NOT_REPLAY`. After an uncertain submission, reconcile the
original request/run within the same process; never create a new request ID or
automatically resubmit after restart. `/identity` reveals process changes. Unknown
reviewer/admin run reads return `404 RUN_NOT_FOUND_OR_RESTARTED`; customer reads
preserve nondisclosure. There is no durable deduplication or workflow recovery.

Authentication and observer context are copied into the worker. Optional passive
callbacks publish real node starts/completions/interrupts, including repeated
`human_review` entry on resume. Tool events contain only actual dispatcher results:
allowlisted names, success/error, proposal linkage, and validated RAG references
when present. Calls rejected by graph checks are not invented tool executions.
No raw prompts, model reasoning, checkpoint data, generic tool arguments/results,
or internal audit objects are serialized. Sink failures cannot replay operations.
RAG scores describe local retrieval similarity, not confidence; excerpts are
untrusted text and local source URIs are identifiers, not executable links.

Consumers deduplicate by `(run_id, sequence)`, use `next_after`, and reconcile gaps
against the returned snapshot. A cursor ahead of the server returns 409. Empty
events while a provider is blocked mean no new observed activity. Browser
disconnects neither cancel nor restart a run.

Workflow decisions must use the run decision endpoint. Existing standalone
proposal approve/reject endpoints remain available and do not resume a workflow.
A direct or concurrent review is detected by the adapter and existing HITL checks;
do not call both paths for one decision. The snapshot's `review` is the original
binding; `status`, `reviewed_status`, and the existing proposal GET determine the
current state. A memory-write failure can accompany `REVIEW_REQUIRED` or
`REVIEWED`; it does not undo or authorize replay of the committed business change.
Approval/rejection executes no refund, payment, order, inventory, or external action.

## Focused verification and limits

Command: `./backend/.venv/Scripts/python.exe -B -W error::UserWarning -m unittest discover -s tests -p test_frontend_contracts.py -v`.

Result: **16/16 passed**, with serialization warnings treated as errors. Only the
new focused suite ran. It covers auth/ownership, closed inputs,
unavailability, live polling during a blocked provider, concurrent deduplication,
bounded admission/retention, event pagination/gaps, review binding/concurrency,
conversation binding/history, independent memory failures, safe RAG projections,
guardrail/tool/provider failures, sanitized diagnostics, and passive sink failure.
Network/DNS calls are blocked. No full regression, frontend build, live provider,
Docker runtime, or AWS verification is claimed.

Domain data, workflows, telemetry, and request IDs remain in memory. SQLite stores
conversation text only. History pagination bounds HTTP responses; underlying
Phase 12 history/list reads still use the existing store's materialization limits.
This is a single-process local integration, not durable recovery, multi-worker
coordination, cross-store transactions, complete audit retention, or production
authentication. Provider code must be bounded/cooperative; Python threads do not
forcibly cancel a hung trusted provider.
