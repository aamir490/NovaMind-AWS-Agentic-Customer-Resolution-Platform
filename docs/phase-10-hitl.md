# Phase 10 — Human-in-the-loop proposal review

**Phase 10 complete**, following independent verification reported by the user. This document records its historical contracts and verification. Phase 13 now requires an authenticated scope for HITL, restricts resume/audit to REVIEWER or ADMIN, removes `reviewer_name` from `HumanDecision`, and derives reviewer identity from context. See [current authenticated usage and limits](phase-13-security.md). The earlier examples and unauthenticated-identity descriptions below are historical; checkpoint, binding, transition, and no-execution boundaries remain intact. Phase 14 has NOT started.

## Architecture and checkpoint state

`backend/app/hitl.py` adds `HITLWorkflow`. It composes the existing Phase 9 node implementations with `prepare_review` and `human_review` nodes. Phases 8 and 9, existing tools, LLM providers, and business services remain unchanged. No new dependency is required; LangGraph's installed `InMemorySaver`, `interrupt`, and `Command` provide the local boundary. See the official [interrupt/resume documentation](https://docs.langchain.com/oss/python/langgraph/interrupts).

`HITLState` contains the server-generated workflow ID, nested Phase 9 `GraphState`, ordered workflow audit events, review payload, reviewed status, and error. Updates are validated and serialized to JSON-compatible values before checkpointing; strict MessagePack deserialization is tested. Checkpoints and a local run-status registry belong to one `HITLWorkflow` instance. No database, durable store, external tracing, or cross-process state is configured.

## Interrupt condition and flow

```text
Phase 9 load/reason/tool/record nodes
  -> successful creation of a case-bound PENDING_REVIEW proposal
  -> prepare_review: confirm live proposal, create review ID, record pause event
  -> human_review: interrupt(minimal review payload)
  -> external resume with validated HumanDecision
  -> existing ProposalService.review
  -> deterministic REVIEWED result -> END
```

Creation and pause-event recording occur in prior checkpointed nodes. Resuming restarts the interrupt node, never proposal creation. There is no LLM call after the pause, no model reinterpretation of the decision, and no financial/business action node after review. Merely looking up an existing pending proposal does not create a new HITL pause; this phase pauses on successful creation by the current workflow.

Phase 9's tool allowlist, argument validation, case binding, one-proposal-per-run protection, and maximum LLM-step count remain in effect. A proposal created on the final allowed LLM step still pauses for review: human continuation consumes no LLM steps and goes directly to END. Other tool calls at the limit still fail with `STEP_LIMIT`. This is the intentional Phase 10 extension to Phase 9's last-step behavior.

## Public contracts

`start(AgentRequest)` creates a fresh server-generated workflow ID. Callers cannot supply checkpoint state, history, thread IDs, node routing, or counters. The result is `REVIEW_REQUIRED`, `REVIEWED`, `COMPLETED`, or `FAILED`.

The review payload contains only workflow/case/proposal/review IDs, proposed action, rationale, and `PENDING_REVIEW`. It excludes prompts, conversation history, customer/order records, and reviewer identity. Rationale remains untrusted proposal text. Detailed history is available separately through the local `audit(workflow_id)` diagnostic method.

`resume(HumanDecision)` requires:

- `workflow_id`, `case_id`, `proposal_id`, and server-issued `review_id` matching the active checkpoint.
- `decision`: exactly `APPROVE` or `REJECT`.
- `reviewer_name`: nonblank, maximum 120 characters; a caller-supplied identity label.
- Optional `note`: omitted/null or nonblank, maximum 4000 characters. Omission maps to `No note supplied.` for the existing Phase 4 required-note contract.

Unknown fields, invalid identifiers, invalid decisions, and malformed or tampered model instances are rejected. `ResumeRejected` contains `ok=False` and a safe error code. Rejected validation/binding attempts leave the checkpoint paused and do not call the review service. A valid resume uses `Command(resume=...)` for the bound thread; the interrupt node validates the payload again before invoking `ProposalService.review`.

Example with an existing fake or intentionally configured real `StructuredLLM`:

```python
from backend.app.agent import AgentRequest
from backend.app.hitl import HITLWorkflow, HumanDecision

workflow = HITLWorkflow(llm, app.state.local_tools, app.state.proposal_service)
paused = workflow.start(AgentRequest(case_id=case_id, message="Please review my request"))
if paused.status == "REVIEW_REQUIRED":
    review = paused.review
    # Obtain these decision/identity fields from an explicit human operation.
    # Do not ask the LLM to populate them.
    finished = workflow.resume(HumanDecision(
        workflow_id=review.workflow_id, case_id=review.case_id,
        proposal_id=review.proposal_id, review_id=review.review_id,
        decision="REJECT", reviewer_name="Local reviewer", note="Reviewed evidence",
    ))
    audit = workflow.audit(review.workflow_id)
```

No HTTP endpoint or frontend HITL UI is introduced. Keep the same workflow instance alive to resume it.

## Human trust and replay boundary

The LLM sees only existing Phase 5 tools. Neither `resume` nor proposal review is a tool, and reviewer identity is not an LLM output field. Human `APPROVE` maps directly to Phase 4 `APPROVED`; `REJECT` maps directly to `REJECTED`. The service supplies review timestamps and rejects repeated transitions. Approval is a review record, never action execution.

A process-local lock serializes starts/resumes for one workflow service. The registry accepts resume only while paused; matching IDs bind the request to the correct checkpoint. The proposal must still be pending in the existing service. The atomic Phase 4 transition also protects against concurrent direct reviews. Completed or failed resumes cannot be replayed through this interface. Duplicate proposals are not created on resume.

These IDs provide correlation, not authentication. A caller who has the matching IDs can claim a reviewer name. This local boundary cannot prove that a person supplied the decision, protect against arbitrary Python access to private internals, or provide tenant isolation. Do not expose it as an authenticated approval API without future access controls.

## Audit and failures

The ordered workflow audit contains sequential numbers and server UTC timestamps for Phase 9 agent/tool events, the review-required event, and the actual human decision/status. Successful resume retains the exact prior audit prefix and appends one human-review event. Invalid preflight resume attempts do not append review events or alter the paused checkpoint. Diagnostic audit results are deep copies.

Unexpected workflow errors produce a safe failure event and prevent automatic retries. Proposal review and checkpoint writes are not transactional: if the service updates a proposal and a later operation fails, the proposal may already be terminal even though the workflow reports failure. Inspect local records; there is no recovery/re-execution feature. Direct review through an existing Phase 4 API causes subsequent workflow resume to return `ALREADY_REVIEWED`; automatic reconciliation is not implemented.

## Focused verification and limits

### Independent verification reported by the user

- Phase 10 focused HITL tests: **20/20 passed**.
- Full backend regression: **129/129 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were recorded without rerunning tests or builds during this documentation-only update. They do not establish live Gemini or browser verification.

### Implementation-time focused evidence and preserved limitations

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_hitl.py -v`

**20/20 passed**: real interrupt/checkpoint detection, approve/reject resume, optional note, malformed and tampered payloads, wrong IDs and swapped workflows, replay/concurrency, external review conflicts, ordered audit, model/reviewer separation, unchanged business records, existing failure/step protections, last-step pause, informational completion, missing cases, instance isolation, uncertain-write failure, and strict checkpoint serialization.

During implementation, no live Gemini calls, full regression, frontend build, or browser verification ran; subsequent independent results are recorded above. No packages were installed or changed. Reviewer identity is not authenticated. Checkpoints are in-memory and lost on restart, confined to one instance, and retained without expiry/cleanup. There is no durable recovery or frontend HITL UI. Approval/rejection does not execute business actions. Audit is not durable or tamper-proof. Existing scenario/LLM correctness limitations remain. No RAG, production persistence, AWS/Bedrock, payments, refunds, replacements, returns, or inventory execution was added. Phase 11 requires separate approval.
