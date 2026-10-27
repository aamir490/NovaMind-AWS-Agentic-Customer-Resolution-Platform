# Phase 9 — Explicit LangGraph workflow

Implemented with **25 focused tests passing** using scripted fake providers. Awaiting user review and broader verification. Phase 10 has NOT started.

## Scope and entry point

`backend/app/graph_agent.py` adds `GraphResolutionAgent`. The Phase 8 `ResolutionAgent` and its tests remain unchanged as the behavioral baseline. Both accept the same `AgentRequest`, `AgentConfig`, `StructuredLLM`, and `LocalTools`, and return `AgentResult`. Domain rules and existing tools/providers/services are unchanged.

```python
from backend.app.agent import AgentConfig, AgentRequest
from backend.app.graph_agent import GraphResolutionAgent

# llm is an existing StructuredLLM with a fake provider for tests,
# or the existing GeminiProvider when intentionally making real requests.
# tools is the existing application's app.state.local_tools.
agent = GraphResolutionAgent(llm, tools, AgentConfig(max_steps=8))
result = agent.run(AgentRequest(case_id=case_id, message="Review my case"))
# Use run_state(request) instead to inspect the same run's graph state/node path.
```

No API endpoint, automatic Gemini initialization, frontend wiring, deployment, or provider-native tool calling is added.

## State and nodes

`GraphState` is a Pydantic model with unknown fields forbidden. It contains the validated request, bounded LLM step counter, messages, tool names, current decision, staged audit event, ordered history, observed pending-proposal map, proposal-created flag, error, final result, and node path. Incoming snapshots and outgoing updates are validated. Public entry points accept only a fresh `AgentRequest`, not caller-supplied graph state/history/counters.

```text
START -> load_case
           | error -> fail_safely -> END
           v
         reason
           | error -> fail_safely -> END
           | final -> finalize -> END
           | tool
           v
       execute_tool -> record_result
                          | error/limit -> fail_safely -> END
                          | success -> reason
```

- `load_case`: validates/loads the requested case through `get_case`, records step 0, and builds the existing tool catalog and prompt.
- `reason`: checks the LLM-call budget and requests the existing validated `AgentDecision` through Phase 6.
- `execute_tool`: enforces the allowlist, case-bound proposal creation, and one-proposal limit; calls only the Phase 5 dispatcher, which validates tool inputs before service execution.
- `record_result`: appends the staged tool outcome once, tracks pending proposal observations, and feeds the structured result back to the LLM. It terminates when the application step limit is reached.
- `finalize`: records the final decision and renders the same informational/pending-review response as Phase 8.
- `fail_safely`: returns a failed result with retained audit evidence and pending proposals. It records a staged completed result without re-executing its tool if graph execution fails between execution and recording.

Implementation follows the official [LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), using `StateGraph`, conditional edges, and local state updates.

## Bounds and safety

The original step semantics remain: default eight LLM calls, configurable 1–32; one tool call at most per reasoning step; preflight is step 0. Final on the last step succeeds. A tool call on the last step is recorded, then returns `STEP_LIMIT` without another LLM call. LangGraph's separate recursion limit is `3 * max_steps + 5`, allowing graph-node overhead while keeping a second execution bound.

Unknown tools, malformed decisions/arguments, missing records, conflicts, provider errors, and step exhaustion terminate safely. Engine failures use `GRAPH_ERROR` or `GRAPH_LIMIT` with the last completed snapshot. Unexpected exceptions are not exposed to the customer. No retry policy, checkpointer, store, interrupts, or resume flow is configured. LangSmith tracing is explicitly disabled, including when enabled in inherited environment settings.

Only the eight existing tools are available: customer/order/case lookup, inventory, policy, eligibility, pending proposal creation, and proposal status. Business checks remain in existing services. No node can approve/reject proposals or execute refunds, returns, replacements, inventory changes, or arbitrary model-selected functions. Proposal status must be `PENDING_REVIEW` on creation. Fixed customer wording distinguishes informational results from proposals requiring separate human review; no HITL implementation was added.

## Dependencies

Only **`langgraph==1.2.12`** was added as a direct dependency. Its 19 newly required transitive packages are pinned in the existing requirements snapshot; existing package versions were preserved. These include `langchain-core`, `langgraph-checkpoint`, `langgraph-prebuilt`, `langgraph-sdk`, and `langsmith`. Their installation does not enable hosted services, persistence, prebuilt agents, or telemetry. No separate LangChain application integration or unrelated package was added.

## Focused verification

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_graph_agent.py -v`

**25/25 passed**: the 15 Phase 8 behavioral scenarios executed against the graph, plus 10 graph-specific tests for routing, ordered audit recording, last-step success, 32-step termination, baseline result/prompt parity, proposal parity, validated state, engine failure recovery, staged-write recovery, and disabled external tracing. Providers are scripted fakes; parity tests intentionally call the unchanged Phase 8 implementation for comparison. No other test suite ran.

`pip check` passed. No live Gemini request, full regression, frontend build, or browser verification was performed. Package download and official documentation lookup occurred during implementation.

## Limitations

State and audit history exist only for a run and are not durable or tamper-proof. A process crash can lose history; writes are not transactional with graph updates. A failure before a completed outcome reaches the graph snapshot may leave an uncertain write outcome. Inspect records before manually retrying. No automatic recovery or cross-run idempotency exists.

Phase 8 limitations remain: no authentication/access control, no guaranteed completeness or truth of model reasoning, no verified scenario inputs, and no live-provider evaluation. Steps bound call count, not total latency or prompt size. A graph can finish early with limited evidence. There is no HITL pause/resume, memory, RAG, database, AWS/Bedrock, or financial execution. Phase 10 requires separate approval.
