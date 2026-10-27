# Phase 16 — Observability & Auditability

Local implementation with **18/18 focused tests passed** on October 2, 2026. Phase 1–15 business, security, orchestration, and evaluation boundaries are retained. No full regression, frontend verification, live provider/cloud call, dependency change, or commit/push is claimed. These are historical results; subsequent [Phase 17 packaging](phase-17-containers.md) preserves these application boundaries.

## Implemented capabilities

`backend/app/observability.py` provides a standard-library local observer, closed-schema JSON events, context-scoped trace/span IDs, bounded event retention, and aggregate metrics. Passive decorators instrument the loop agent, graph agent, tools, structured model generation, eligibility assessment, proposal creation/review, and HITL start/resume. They preserve the original result or exception and never retry a business operation.

HTTP middleware wraps authentication as well as routing. Each request receives a new server-generated trace ID; normal and handled-error responses include `X-Trace-ID`. Client trace headers are ignored. HTTP events contain method, status code, latency, and a bounded error code; raw paths, URL/query parameters, headers, bodies, and response text are omitted. Unhandled exceptions are recorded with status 500 and re-raised for existing FastAPI handling; the outer framework's generated 500 response may not contain `X-Trace-ID`.

Nested calls share a trace ID and carry separate span IDs and parent span IDs. Identity and tracing use separate contexts. Concurrent requests/threads do not share a current trace. Context resets on scope exit, including failure. Existing LangGraph/LangSmith tracing remains disabled; no exporter, endpoint, remote collector, tracing service, or network client is added.

## Events and metrics

Events contain a UTC completion timestamp, allowlisted operation, trace/span/parent IDs, nonnegative elapsed milliseconds, and a fixed error code or null. Optional metadata is restricted to known tool names, provider family, known result status, method/status code, token counters, and selected audit UUIDs. Unknown tool names become `unknown`; unknown failure codes become `ERROR`.

- **Structured application logs:** JSON events can be emitted through the dedicated `novamind.telemetry` logger. It has a null handler and propagation disabled by default. A trusted host explicitly configures a local handler and level; this phase adds no log files or logging server.
- **Agent/tool tracing:** spans show loop/graph runs and nested tool/model calls. Tool failures, guardrail rejection, ownership denial, bounded-step failures, and structured-output failure retain their existing application behavior. No tool arguments, results, decisions, histories, prompts, or retrieved/memory text are serialized into telemetry.
- **Error/latency metrics:** each operation has a count, error count, total elapsed milliseconds, and maximum elapsed milliseconds. A normal policy denial is a successful eligibility assessment, not a technical error. Errors at multiple nested boundaries are counted per operation, not deduplicated into a global incident count. Nested durations overlap and must not be summed as total application wall time.
- **Model usage:** structured model events distinguish a provider call from a request rejected before provider invocation. Provider family is bounded to `gemini`, `fake`, or `other`; raw configured model names are omitted. Per-model/version breakdown, cost, and billing reconciliation are not implemented.
- **Token usage:** `LLMResponse` adds optional `TokenUsage` with independently nullable input/output/total counters. Absent counts are unknown, not zero. Validated counters are nonnegative strict integers up to one billion. Each metric records known token sums and the number of provider calls with unknown counts. Output-token configuration is logged separately as a limit, never as usage. Refusal or invalid output can still consume known tokens. Total tokens are kept independent from input/output because provider accounting may include other categories.
- **Gemini metadata:** the existing adapter maps optional SDK prompt/candidate/total token counts into the neutral envelope. Invalid/missing metadata becomes unknown without changing completion/error handling. Focused tests inject mocked clients; no live Gemini call occurs.
- **Audit and human review:** successful proposal create/review events contain proposal and case UUIDs and the resulting status. Review actor UUIDs come only from current authenticated REVIEWER/ADMIN context, never from the supplied `reviewer_name` or notes. Trusted internal review without that context is marked `reviewer_authenticated=False` and receives no actor UUID. HITL start/resume events carry workflow UUIDs from returned results, linking separate request traces without persisting or restoring identity/trace authority. Rejected resumes do not emit a successful review event. Existing proposal records and HITL audit history remain authoritative and unchanged.

Default retention is **1,000 completed events per observer**, configurable from 1 to 100,000. Old events are evicted with an explicit `evicted_events` count. Aggregate metrics survive eviction but reset with the observer/process. Metric keys use ten fixed operation labels, avoiding user-controlled label cardinality. Snapshots are isolated copies. A lock protects event/metric updates across threads.

## Local setup and access

`create_app()` owns an independent observer at `app.state.observer`. A trusted host may inject a shared/configured observer with `create_app(auth_provider=..., observer=...)`. There is no diagnostics HTTP endpoint and no anonymous fallback.

Programmatic agents/tools/HITL use an explicit observer scope in addition to the existing authentication scope:

```python
from backend.app.observability import LocalObserver, observing
from backend.app.security import authenticated

observer = LocalObserver(capacity=1000)
with authenticated(auth_provider, customer_token), observing(observer):
    result = agent.run(request)

with authenticated(auth_provider, reviewer_token):
    diagnostics = observer.snapshot()
```

`snapshot()` requires a fresh REVIEWER or ADMIN identity, matching the existing diagnostic audit permission. A customer or unauthenticated caller cannot read it. Server trace IDs prove no identity and authorize no action. To enable local JSON output, the trusted host may attach a standard `logging.StreamHandler` to `novamind.telemetry` and set its level to `logging.INFO`. Do not configure a remote handler in this local phase. Host log storage/access/retention must be managed separately; the ring-buffer cap does not cap a host-provided log sink.

Telemetry is best effort. Observer/logging failures do not fail or repeat agent, proposal, or review operations. Local logger failures increment `logging_failures` when possible. There is no rollback, automatic retry, or transaction spanning telemetry and business records. The observer is a trusted host facility, not a sandbox against arbitrary Python code; its objects/scopes must never be exposed as model tools or populated from customer payloads.

## Focused verification

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_observability.py -v
```

**18/18 passed.** `git diff --check` passed with line-ending normalization warnings only. The suite uses Phase 15 fixture helpers, scripted providers, mocked Gemini clients, temporary SQLite stores, and in-process HTTPX ASGI transport. Socket/DNS operations are blocked and asserted unused. Importing fixture helpers does not run the Phase 15 evaluation or any earlier test suite.

Coverage includes loop/graph parentage and unchanged outcomes; HTTP authentication/ownership/validation/500 errors; concurrent request/thread isolation; metadata/credential/text exclusion; reviewer-only snapshots; partial/missing/zero/known token accounting; pre-provider rejection and consumed tokens on model failures; mocked Gemini metadata; preserved unexpected exceptions; bounded retention and exact synthetic-clock latency totals; safe snapshots; local JSON logging; broken sinks without retry; proposal/review and eligibility tracing; and HITL pause, fresh reviewer authorization, replay protection, linked workflow history, and no execution. Asyncio may print slow-callback diagnostics during local HTTP tests; these are not test failures.

Only Phase 16 focused tests ran. No full regression, Phase 15 evaluation run, frontend tests/build, live model/network/AWS calls, or package installation ran. Earlier phase counts remain historical evidence.

## Files and retained limitations

Added `backend/app/observability.py` and `tests/test_observability.py`. Instrumented `main.py`, `agent.py`, `graph_agent.py`, `tools.py`, `llm.py`, `gemini.py`, `operations.py`, `proposals.py`, and `hitl.py`. Updated root/backend/test READMEs, both roadmaps, the Phase 15 status pointer, and this guide. No dependencies changed; the three untracked root JSON files are untouched.

This is local, in-memory, best-effort observability, not durable, complete, tamper-evident compliance auditing or production monitoring. There are no dashboards, alerts, distributed context propagation, sampling, percentile histograms, cross-process aggregation, cloud exports, metrics endpoints, or deployment changes. HTTP latency measures middleware processing until response headers are available, not the entire streamed response or background tasks. Exceptions after that boundary are not covered by the request span. Event ordering is completion order; parent IDs establish nesting. Event eviction can remove earlier trace or review evidence.

The new event schema excludes free text but does not redact existing Phase 8–15 diagnostic histories, checkpoints, third-party loggers, or host-controlled sinks. Audit UUIDs remain sensitive metadata and are available only through authenticated local diagnostics or trusted host logging. Token totals with unknown calls are partial, not complete billing estimates. SDK metadata behavior remains unverified against a live provider.

Authentication, customer ownership, deterministic policies, allowlists, guardrail limits, case/proposal/step bounds, authenticated HITL, and no-execution behavior remain authoritative. SQLite still persists conversation text only; business stores, HITL checkpoints, and existing audit history remain in memory. These limits also apply to subsequent [Phase 17 containers](phase-17-containers.md).
