# Phase 8 — Controlled customer resolution agent

**Phase 8 complete**, as directed by the user following final verification. Focused agent tests use scripted fake providers. Live Gemini and browser verification remain unverified. Phase 9 is next, pending approval; it has not started.

## Architecture and entry point

`backend/app/agent.py` is an in-process orchestration layer. It depends on `StructuredLLM` and `LocalTools`, not domain rules or Gemini-specific APIs. Existing providers, tools, services, endpoints, and business rules are unchanged.

```text
Validated AgentRequest (case_id + customer message)
  -> Phase 5 get_case preflight (audit step 0)
  -> Phase 6 StructuredLLM validates one AgentDecision
  -> either final, or an allowlisted name + argument object
  -> Phase 5 validates the tool-specific input and invokes its service
  -> append structured call/result to this run's history
  -> feed tool result to the next LLM call
  -> repeat within AgentConfig.max_steps, or fail closed
```

Each loop step makes one LLM call and at most one tool call. The default limit is eight steps; configurable integer limits are 1–32. Case preflight is recorded as step 0 and does not consume an LLM step. A final decision on the last permitted step succeeds. A tool decision on that step executes, but without a subsequent final decision the run ends as `FAILED` / `STEP_LIMIT`. Earlier proposal writes are not rolled back.

To compose with the existing Gemini provider, use an existing application's tool store and a case ID from that store:

```python
from backend.app.agent import AgentConfig, AgentRequest, ResolutionAgent
from backend.app.gemini import GeminiProvider
from backend.app.llm import StructuredLLM

# app and case_id refer to an existing in-process application and support case.
# This explicitly initiated run makes real Gemini requests; it was not run in verification.
with GeminiProvider() as provider:
    agent = ResolutionAgent(StructuredLLM(provider), app.state.local_tools, AgentConfig(max_steps=8))
    result = agent.run(AgentRequest(case_id=case_id, message="Please review my wrong-item request."))
```

Gemini configuration remains as described in [Phase 7A](phase-7a-gemini.md). There is no automatic provider initialization, agent HTTP endpoint, frontend change, or SDK-native function calling. Tool decisions are validated JSON interpreted by this bounded application loop.

## Available tools and controls

The tool catalog comes directly from Phase 5: `get_customer`, `get_order`, `get_case`, `get_inventory`, `get_policy`, `assess_eligibility`, `create_resolution_proposal`, and `get_proposal_status`.

Every model decision is validated through Phase 6 before dispatch. Unknown names terminate without calling the dispatcher. Tool-specific validation is performed by `LocalTools` before any business service runs. Malformed arguments, lookup failures, and conflicts terminate the run with audit evidence. There is no arbitrary function lookup, eval, parallel execution, or automatic retry.

Proposal creation is restricted to the request's case and at most one successful creation per run. The existing proposal service creates `PENDING_REVIEW`; the agent checks this postcondition. It cannot approve/reject or execute refunds, returns, replacements, or inventory changes. These operations are absent from the allowlist. The agent does not duplicate eligibility rules or add a new eligibility prerequisite for recording a proposal.

Customer input and record text are marked as untrusted in the system prompt. This instruction is not a security boundary; dispatch restrictions and existing validation enforce the available operations. Generated assessment inputs can still be inaccurate or fabricated and remain unverified scenario inputs.

## Result and audit contract

`AgentResult` includes `INFORMATIONAL`, `HUMAN_REVIEW_REQUIRED`, or `FAILED`, fixed customer-facing wording, steps used, ordered history, observed pending proposal IDs, an optional error code, and `actions_executed=False`.

Customer wording is application-rendered, not free-form model text. Tool data, including context, policy, eligibility assumptions, and denial reasons, remains available in the structured history. The LLM chooses when to finish but cannot write a customer claim that an action was executed. Pending proposals are explicitly labelled as requiring separate human review, including when a later step fails. Observed statuses are snapshots, not a guarantee against concurrent human review.

History records validated model decisions, tool results/errors, and Phase 6 failure codes. Raw invalid model output is not retained because the existing Phase 6 boundary returns only a safe validation failure. Unexpected exceptions produce generic `LLM_ERROR` / `TOOL_ERROR` events without exception details. An unexpected write failure may have an uncertain outcome; inspect local records before manually retrying. No automatic retries occur.

## Limits

- Audit history is returned per run, not persisted or protected against caller modification. There is no cross-run memory, durable audit log, or checkpointing.
- One-proposal limits are per run, not global idempotency; another run may create another proposal. A run does not discover all existing proposals automatically.
- Case binding for writes is not authentication. Read tools retain Phase 5 access semantics; no tenant/customer access control is added. Do not expose this as an authenticated customer API without future access controls.
- A model may finish early with limited evidence. `INFORMATIONAL` does not certify a complete investigation or resolve the case. The original case status is unchanged.
- Step limits bound call count, not total elapsed time or prompt size. Real model schema compatibility, reasoning quality, prompt-injection resistance, latency, and cost remain unverified.
- No LangGraph, RAG, database, AWS/Bedrock, infrastructure, dependencies, or financial execution was added.

## Focused verification

### Final verification reported by the user

- Phase 8 focused agent tests: **15/15 passed**.
- Full backend regression: **84/84 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were recorded without rerunning tests or builds during this documentation-only update.

### Implementation-time focused test evidence

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_agent.py -v`

**15/15 passed**: loop feedback and audit serialization; read-tool delegation and denial reasons; pending proposal/status flow; forbidden names; malformed decisions; field injection; invalid/missing records; case binding; duplicate proposal limits; step exhaustion; failure after a write; provider/tool failures; missing-case preflight; request/config validation; and per-run state isolation. No live API requests were made.
