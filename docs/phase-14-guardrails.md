# Phase 14 — Guardrails & AI Safety Controls

Phase 14 implements local safeguards around the existing Phase 1–13 boundaries. **22/22 focused local tests passed.** It introduced no business actions, provider integration, evaluation framework, or Phase 15 work. These are historical Phase 14 results; [Phase 15](phase-15-evaluations.md) now evaluates the existing components without changing these safeguards. See the [phase scope](PROJECT-PHASE-ROADMAP.md).

## Controls and integration

- **Input validation:** existing strict request schemas remain. After authenticated case preflight, both agent implementations check customer input before model invocation or conversation creation/append. HITL delegates to the same graph nodes.
- **Prompt-injection defenses:** deterministic checks inspect request text, selected persisted memory, tool arguments, and validated tool results, including retrieved chunks and their metadata. Checks reject explicit instruction overrides, authentication/human-review bypass instructions, and selected system/developer role delimiters. Matching uses Unicode NFKC normalization, removal of format characters, and whitespace normalization; original content is never rewritten. System instructions explicitly label request, memory, and tool-result JSON as untrusted data.
- **Output validation:** the provider-neutral structured LLM boundary rejects duplicate JSON keys, non-finite values, excessive size/depth, malformed JSON, extra fields, and incorrect decision shapes. Existing strict Pydantic validation still runs on the original JSON. Provider request/response model instances are revalidated. Final customer text remains application-rendered; models cannot return arbitrary customer wording or claim an executed action through the final-decision contract.
- **Tool allowlists and arguments:** the same nine tools remain available. Unknown or sensitive tool names fail without dispatch. Argument shape/size checks precede validation; ownership checks precede content screening and service invocation. Model-provided status/reviewer/authority fields remain forbidden. Tool validation errors expose at most eight error types, without echoing attacker-controlled keys or values.
- **Tool output checks:** outputs are revalidated from their fields even if an adapter returns a preconstructed model instance, then checked before exposure to the next model call. Invalid outputs return a fixed `INVALID_OUTPUT` failure. Rejected tool results contain no data payload.
- **Sensitive actions and policy enforcement:** existing deterministic eligibility and ownership checks remain authoritative. Guardrails do not replace those rules or make pending proposals evidence of eligibility. Only one case-bound pending proposal can be created per agent run. Review requires a fresh authenticated REVIEWER/ADMIN context, matching workflow/case/proposal/review IDs, and a server-derived reviewer UUID. Approval still executes nothing.
- **Model and tool failures:** existing refusal, truncation, provider timeout/unavailability, unexpected exception, tool failure, and step-limit paths stop without automatic retry. Pending proposals already recorded before a later failure remain pending and are reported by the existing agent result. No business write or review is replayed to repair a model or persistence failure.

`guardrails.py` has no provider, business-service, storage, or authentication dependency. The loop and graph import it at their existing boundaries; HITL inherits the graph checks without new nodes, checkpoint fields, or resume authority. The conversation service checks selected memory before appending the current turn. Raw memory storage remains a text store: storing a string does not authorize executing or sending it to a model.

## Local limits and failures

JSON payloads allow at most **65,536 serialized characters**, **16 nesting levels** (root at zero), and **4,096 visited nodes**, including dictionary keys. Strings also share a 65,536-character pre-serialization budget. Model response text has the same 65,536-character limit. Entire model message content is limited to **262,144 characters** before provider invocation; existing per-field limits, output-token limits, retrieval bounds, memory selection limits, and maximum reasoning steps continue to apply.

- `UNSAFE_CONTENT`: a supported injection pattern was detected. The agent stops safely; rejected request text is not appended to memory, and rejected selected memory does not receive the current turn.
- `PAYLOAD_LIMIT`: a tool/request/memory payload exceeds structural or size limits.
- `INVALID_INPUT`: existing schema failure or a non-JSON/non-finite tool argument.
- `INVALID_OUTPUT`: tool output does not satisfy its declared schema.
- `INPUT_LIMIT`: model context exceeds the aggregate character budget; the provider is not invoked.
- `INVALID_RESPONSE`: malformed, ambiguous, oversized, deeply nested, or schema-invalid model output. Refusal/truncation retain their existing explicit codes.

Authentication and case preflight still precede request-content checks. Authentication remains required before any tool lookup. HTTP authorization, role permissions, reviewer identity, and conversation/case ownership are unchanged. A safety error is never authentication or business authorization.

## Focused local verification

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_guardrails.py -v
```

**22/22 passed on October 2, 2026.** `git diff --check` passed with CRLF/LF normalization warnings only. The initial run found one test-fixture context-manager error, which was corrected before the passing run; no product failure was observed in that run.

The suite uses scripted providers, existing local service fixtures, temporary SQLite/corpus files, and a socket-connect block. It exercises the loop, graph, and HITL paths; benign requests and memory; customer/case/memory/RAG/model injection; Unicode and role spoofing; argument and output validation; JSON ambiguity and limits; safe errors; model failure/refusal/truncation; policy denial; tool restrictions; step/proposal/case bounds; cross-customer denial; missing identity; authenticated review and replay rejection; and retention of a pending proposal after a later model failure.

Only this Phase 14 suite is run. Importing existing fixture helpers does not execute their test suites. No full regression, frontend test/build, live Gemini/network/AWS call, package installation, or commit/push is claimed. Earlier phase test counts remain historical evidence. Some older injection fixtures expect untrusted text to reach the model; the new explicit rejection behavior supersedes that expectation. Those suites are not rerun or rewritten in this scope.

## Dependencies, boundaries, and limitations

No dependency changes. Checks use the Python standard library and existing Pydantic, LangGraph, and application services. The three pre-existing untracked root JSON files are untouched. No frontend, HTTP endpoint, business rule, role, tool capability, provider selection, infrastructure, or evaluation dataset is added.

These are local heuristic defenses, not proof against prompt injection or production safety certification. Novel, encoded, split-across-fields/chunks, or non-English instructions can evade matching; benign quoted examples or negated bypass instructions can be rejected. Only the selected memory/retrieval content is checked, and stored text is not purged or rewritten. A blocked conversation requires a separate clean conversation or trusted host remediation; no remediation UI is added. Final templates constrain claims but do not establish semantic truth of model-generated proposal rationale or caller-supplied assessment facts.

Payload limits bound application processing; they are not transport download limits, token-exact budgets, process isolation, or a timeout for arbitrary host code. No remote safety classifier, PII redaction, malware scanning, or cloud guardrail service is introduced. Existing diagnostic audit/checkpoint data may retain rejected model decisions and must remain untrusted; this phase does not add audit redaction. A tool output failure after a trusted adapter has written data does not roll back that write; callers must inspect current records before retrying. Domain services and raw adapters remain trusted Python components, not sandboxes.

SQLite continues to persist conversation text only. Domain data, workflow checkpoints, and audit history remain in memory; no cross-store transactions, restart recovery, token lifecycle, frontend login, or production deployment is added. These limitations remain in the subsequent [Phase 15 evaluation](phase-15-evaluations.md).
