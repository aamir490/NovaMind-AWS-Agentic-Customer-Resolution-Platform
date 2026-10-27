# Phase 15 — AI Evaluation & Testing

Local Phase 15 implementation is complete with **15/15 focused tests passed** and **54/54 deterministic evaluation scenarios passed** on October 2, 2026. No broader regression, frontend verification, live model verification, production readiness, or commit/push completion is claimed. These are historical results; subsequent [Phase 16 observability](phase-16-observability.md) preserves the evaluation framework and security boundaries.

## Approach and scope

The `phase15-v1` dataset contains explicit scenario expectations independent of observed results. The harness drives the existing Phase 8 loop, Phase 9 graph, and Phase 10 HITL workflow through the Phase 6 structured LLM interface with an evaluation-only scripted provider. Business rules, tools, authentication, ownership, retrieval, guardrails, conversation storage, and review are the existing Phase 1–14 implementations. No application module or dependency changes are needed.

Each agent/HITL scenario gets fresh business stores, two customers, server-configured local CUSTOMER/REVIEWER credentials, and an isolated temporary SQLite database. Malicious retrieval uses a temporary synthetic corpus; normal retrieval uses `backend/knowledge/support.json`. No real customer data or production credential is required. Runtime-generated IDs/timestamps are excluded from scoring reports, so repeated runs produce identical JSON reports.

The harness disables LangSmith tracing and blocks socket connect/connect_ex, connection creation, DNS lookup, and sendto while scenarios execute. Any attempted network operation is recorded and fails the offline check even if application code catches the exception. The provider is always the local scripted class; there is no provider selector, Gemini/AWS adapter initialization, model-based judge, or environment-driven remote option. Offline monkeypatches are temporary and restored on exit. Run this harness in its own process; its socket patches are not intended for concurrent use inside a server.

## Dataset and checks

- **38 agent scenarios:** nineteen cases each for the loop and graph. They cover informational completion, the 30/31-day policy boundary, pending proposals, forbidden tools, invalid arguments, unsupported final claims, duplicate JSON keys, timeout/refusal/truncation, customer/memory/retrieval injection, cross-customer access denial, step/proposal limits, failure after a pending write, and retrieval evidence forwarded to the next model request.
- **5 HITL scenarios:** approve, reject, customer-role denial, mismatched review ID, and spoofed reviewer field. Checks include pause before review, pending status, final proposal status, authenticated reviewer identity, replay rejection after valid review, one proposal only, no additional model calls, and unchanged business state.
- **4 RAG scenarios:** three hand-labeled queries targeting intake, policy interpretation, and proposal review, plus a no-match query. Labels reference source document IDs and are not inferred from retrieval results.
- **6 direct guardrail scenarios:** two benign requests, instruction override, system-role spoofing, Unicode-obfuscated override, and oversized payload.
- **1 offline scenario:** zero network attempts across the evaluation run.

Every agent case checks the exact terminal status/error, provider call count, ordered successful-tool sequence (including case preflight), expected number of proposals, pending-only review state, no execution, and unchanged case/order/inventory snapshots. Script exhaustion is a harness failure. A later model failure must preserve an existing pending proposal without replaying its creation.

## Metrics and acceptance

The report includes `passed`, `scenarios.passed/total`, ordered scenario results, and category metrics with `passed`, `total`, and `rate = passed / total`. Categories are response quality, policy correctness, tool selection, tool arguments, retrieval quality, groundedness, hallucination containment, workflow completion, human escalation, failure handling, guardrails, HITL, safety boundaries, bounded execution, structured outputs, and harness integrity.

**Acceptance requires every check in every scenario to pass.** A high overall rate cannot offset a safety failure. Empty datasets, duplicate scenario IDs, empty check lists, and duplicate check names are rejected. Unexpected scenario exceptions become failed harness-integrity checks with no raw exception details. Their scenario remains in the denominator; category check counts may be smaller if execution failed before those checks could be collected, and the whole run still fails. Expected negative cases pass only for their specified rejection code and side-effect constraints; arbitrary failure is not success.

- **Response quality:** exact application-rendered informational/pending/failure wording, using live pending proposal IDs as evidence. This measures template correctness and honest action status, not language fluency, empathy, or usefulness.
- **Policy correctness:** hand-labeled eligibility and denial expectations at 30 and 31 days, plus `authorization_granted=False`. The evaluator does not copy the business-rule algorithm to derive expectations.
- **Tool selection/arguments:** the successful tool sequence must match the case oracle; forbidden tools and authority-field injection must fail with the exact error and no proposal write. This scores orchestration under a scripted decision, not a real model's ability to choose a tool.
- **Groundedness/hallucination containment:** reported proposal IDs must exactly match actual records; customer wording cannot claim unsupported execution; retrieval evidence must reach the next model request unchanged; retrieved source metadata and text spans must match the loaded corpus. No semantic entailment or free-text rationale truth score is claimed.
- **Retrieval quality:** `precision_at_k` is relevant returned chunks divided by all returned chunks; `document_recall_at_k` is unique relevant documents returned divided by labeled relevant documents; reciprocal rank is `1 / rank` of the first relevant chunk (zero if none). This dataset uses **k=1**. Each labeled query requires all three scores to equal **1.0**. The no-match query requires zero hits; undefined precision/recall values are not fabricated as perfect scores. Across the three labeled queries, observed mean precision@1, document recall@1, and MRR are **1.0**; the no-match case abstained correctly.
- **Safety/HITL/failures:** exact denial/review outcomes, authenticated reviewer checks, immutable domain snapshots, bounded model calls, no business action execution, no automatic replay, and rejection of invalid structured outputs.

All 54 scenarios and all checks passed. Category acceptance rates were 1.0 on this small fixed dataset. That is a local regression result, not a general safety, model-accuracy, or production-quality percentage.

## Verification and use

Only the focused Phase 15 unittest suite was executed:

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_phase15_evaluations.py -v
```

**15/15 passed**, including repeated evaluation for deterministic report equality and loop/graph parity. `git diff --check` passed with CRLF/LF normalization warnings only. Negative grader tests deliberately alter expectations, substitute premature final decisions, forge an unsupported response, and check mixed/empty/duplicate/exception results. Metric arithmetic is tested with irrelevant, repeated, missing, and absent retrieval evidence. Offline-enforcement tests attempt mocked connections/DNS and confirm they are blocked before any network I/O. The CLI's serialization and exit codes are tested using reports supplied by the suite.

The optional `python -B -m evaluations.phase15` entry point prints the detailed JSON report to stdout and exits 0/1 for pass/fail. It does not save a report file or change application data. Earlier Phase 1–14 test counts are historical evidence; their test suites were not rerun. No full regression, frontend test/build, live Gemini/network/AWS call, or package installation ran.

## Files, dependencies, and retained boundaries

- Added `evaluations/__init__.py`, `evaluations/cases.py`, and `evaluations/phase15.py` for versioned cases, isolated fixtures, scoring, offline execution, and reports.
- Added `tests/test_phase15_evaluations.py` for focused behavior and evaluator validation.
- Updated `evaluations/README.md`, root/backend/test READMEs, both project roadmaps, the Phase 14 status pointer, and this guide.

There are **no dependency changes**. The harness uses the Python standard library and existing backend packages. Application code, deterministic policies, role permissions, tool allowlists, ownership checks, structured-output contracts, guardrail rules, HITL review bindings, and persistence architecture remain unchanged. The three untracked root JSON files are untouched. No production logging, telemetry, tracing, metrics service, audit pipeline, or other Phase 16 feature is added.

## Limitations

Scripted decisions cannot measure Gemini/Bedrock response quality, reasoning, stochastic tool selection, or hallucination prevalence. The tiny hand-authored corpus query set is not a representative retrieval benchmark. Guardrail cases cover known supported patterns, not attack detection recall or false-positive rates across real traffic. Fixed response templates constrain rendered claims but do not prove model-generated proposal rationale or customer-supplied facts true. Pending proposals do not establish eligibility or grant authorization.

Focused parity and repeated-run checks are regression evaluation for these scenarios only, not a rerun of all earlier tests. No load, latency, cost, remote timeout transport, concurrency stress, production identity, or cloud integration is evaluated. The socket guard is a test aid, not a sandbox against hostile Python/native code. Existing local-authentication, conversation-only SQLite persistence, in-memory business/checkpoint/audit storage, restart recovery, and no-execution limitations remain. See the subsequent [Phase 16 local observability scope](phase-16-observability.md).
