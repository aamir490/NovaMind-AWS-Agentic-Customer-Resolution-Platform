# Local verification evidence

## Phase 11 — Complete; independent verification reported by the user

- Phase 11 focused Knowledge/RAG tests: **15/15 passed**.
- Full backend regression: **144/144 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These user-supplied results were not rerun during this documentation-only update. Phase 11 is complete; Phase 12 is next but has not started. Retrieval remains local lexical/token-cosine only: no production semantic embedding provider/vector database, live embedding/network calls, or frontend RAG UI. Retrieved content remains untrusted; deterministic business rules and HITL remain authoritative.

### Implementation-time focused local knowledge verification

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_knowledge.py -v`: **15/15 passed** using deterministic retrieval and fake LLMs.
- Covers contracts, corpus/chunk stability, ranking, bounds, safe failures, read-only behavior, Phase 8/9 evidence parity, malicious retrieved instructions, eligibility preservation, and HITL pause/resume with citation metadata.
- During implementation, no new packages, live Gemini/embedding/network calls, full regression, frontend build, or browser checks. Subsequent independent results are recorded above. See [Phase 11 details](phase-11-knowledge.md). Phase 12 has not started.

## Phase 10 — Complete; independent verification reported by the user

- Phase 10 focused HITL tests: **20/20 passed**.
- Full backend regression: **129/129 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These user-supplied results were not rerun during this documentation-only update. Phase 10 is complete; Phase 11 is next but has not started. Reviewer identity remains unauthenticated, checkpoints are in-memory and lost on restart, and there is no durable recovery or frontend HITL UI. Approval/rejection does not execute business actions. Live Gemini and browser verification are not established by these results.

### Implementation-time focused interrupt/resume verification

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_hitl.py -v`: **20/20 passed**, using fake providers and local in-memory checkpoints.
- Covers interrupt/approve/reject, minimal review payload, optional note, malformed/tampered resumes, all ID bindings, replay/concurrent review, external review conflicts, ordered audit, LLM exclusion, unchanged business records, step/failure boundaries, isolated instances, uncertain-write failure, and strict checkpoint serialization.
- Initial checkpoint type warnings were resolved by JSON-compatible state updates; the final focused run passed without those warnings.
- During implementation, no packages, live Gemini calls, full regression, frontend build, or browser verification. Subsequent independent results are recorded above. Phases 8 and 9 remain unchanged. See [Phase 10 limits](phase-10-hitl.md). Phase 11 has not started.

## Phase 9 — Focused graph verification only

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_graph_agent.py -v`: **25/25 passed** with fake providers.
- Includes 15 baseline behavioral scenarios run against the graph and 10 graph-specific routing, parity, state-validation, limit, failure-recovery, staged-write, and tracing tests.
- Dependency health: `pip check` passed. No full regression, frontend build, browser verification, or live Gemini calls ran. Phase 8 remains unchanged; Phase 10 has not started. See [Phase 9 details](phase-9-langgraph.md).

## Phase 8 — Complete; final verification reported by the user

- Phase 8 focused agent tests: **15/15 passed**.
- Full backend regression: **84/84 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were supplied by the user and were not rerun during this documentation-only update. Phase 8 is complete; Phase 9 is next but has not started. Live Gemini and browser verification are not established by these results.

### Implementation-time focused orchestration verification

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_agent.py -v`: **15/15 passed** with scripted fake providers and existing in-memory tools.
- Verified bounded loops, tool feedback, structured history, denied names/malformed inputs, pending-only proposals, case binding, per-run proposal limits, partial-write reporting, failure handling, and isolated run state.
- During implementation, no live Gemini request, full regression, frontend build, browser verification, or dependency installation was performed. Subsequent user-reported verification is recorded above. See [Phase 8 limits](phase-8-agent.md).

## Phase 7A — Final verification reported by the user

- Full backend regression: **69/69 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were supplied by the user after Phase 7A implementation; they were not rerun during this documentation-only update. The earlier 11 focused Gemini tests used mocked/injected clients. Live Gemini and browser verification are not established by these results. Phase 8 has not started.

## Phase 5 — Focused local tool verification only

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_tools.py -v`: **12 tests passed**.
- Verified the exact allowlist, schema descriptions, input validation before service calls, delegated lookups/eligibility/proposal creation, structured expected errors, pending-only creation, exclusion of review/execution, status after separate human review, unchanged case/order/inventory records, and application isolation.
- Unexpected service failures propagate rather than becoming fabricated business results.
- Tests used in-process services only. No full regression, frontend build, browser verification, package installation, or external service/AWS call was performed. Earlier checks below remain historical evidence.

See [Phase 5 contracts and limitations](phase-5-tools.md). Phase 6 has not started.

## Phase 4 — Focused proposal/review verification only

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_proposals.py -v`: **11 tests passed** (7 service tests, 4 HTTP tests).
- Verified pending creation, existing-case linkage, retrieval/filtering, immutable proposed content, server-managed fields, both explicit decisions, terminal/repeated-transition rejection, concurrent reviews with exactly one winner, missing records, invalid inputs, application isolation, and unchanged case/order/inventory records after approval.
- Only the new proposal tests ran. The HTTP harness used a temporary loopback port and shut down after completion. No frontend builds, browser verification, or full regression were run, per scope.
- No new packages, agents, external services, authentication, persistence, execution, or infrastructure were introduced. Previous results below remain historical evidence, not a claim of a Phase 4 full regression.

See [Phase 4 limitations](phase-4-proposals.md), particularly the distinction between a caller-labelled human review and authenticated authorization.

## Phase 3 — Local case review UI

- `node --test tests/frontend-api.test.mjs`: **6 frontend API tests passed** using the built-in Node test runner.
- Existing Python unittest suite: **25 tests passed**. No backend implementation changed.
- Offline frontend production build: passed; 17 modules transformed.
- Manual browser flow: empty-case guidance, refresh/select/load, customer/order context, inventory/policy display, explicit assessment inputs, conditionally eligible day-30 result, and ineligible day-31 result with denial text/code and API-echoed inputs all worked.
- Changing inputs or selecting another SKU cleared the previous result. Unknown inventory produced an alert, not zero stock; selecting a known SKU recovered. `LAP-2` displayed zero stock distinctly.
- After browser assessments, the prepared case remained open with unchanged timestamps and `LAP-1` stock remained five units. Synthetic records were prepared through existing Phase 1 endpoints only for verification; the new UI uses GET requests exclusively.
- All 20 Markdown files passed local-link checks. No packages were added or installed. Verification servers were stopped afterward.

These are local demonstrations, not production, security, accessibility-certification, or automated end-to-end browser tests. The six frontend tests mock transport; rendered UI behavior was verified manually. Phase 4 was not started.

## Phase 2 — Deterministic business operations

- Existing-environment unittest discovery: **25 tests passed**, including all 14 Phase 1 tests and 11 new business-rule/HTTP tests.
- `pip check`: no broken requirements.
- Frontend `npm.cmd run build` in offline mode: passed; 15 modules transformed.
- Local Vite proxy smoke check: health, inventory, policy lookup, and eligibility passed. Day 30 was eligible; day 31 was ineligible with `outside_return_window`; authorization remained false.
- No dependencies were added or installed. Phase 2 uses local fixtures and the existing Phase 1 store only. The frontend changed only its phase label/title and explanatory copy; no business-operations UI was added.
- Verification servers were stopped after checks. No AWS, agent, authentication, database, payment/refund execution, or infrastructure operation was introduced.

Eligibility results are conditional on unverified caller-supplied facts under a demonstration policy. These checks do not prove real-world entitlement or production readiness. See [Phase 2 contracts and limits](phase-2-business-operations.md).

## Phase 1 — Application domain foundation

- `backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -v`: **14 tests passed** (8 domain/service tests and 6 real HTTP tests). Coverage includes validation, missing records, order/customer consistency, the status transition matrix, immutable records, isolated app stores, HTTP error codes, and customer/order/case round trips.
- `backend/.venv/Scripts/python.exe -B -m pip check`: no broken requirements.
- `npm.cmd run build` from `frontend/`, using npm offline mode: passed; 15 modules transformed.
- Loopback smoke check through Vite: health returned the original response; customer/order/case creation succeeded; status changed from `open` to `in_review`; direct backend retrieval returned the same updated case.
- Browser: the Phase 1 welcome page rendered and **Check local API** displayed the successful connectivity message.
- Local documentation links: all 18 Markdown files passed target checks.

No new packages or external services were used. The test server cleans itself up; manual verification servers were stopped afterward. Local example records were temporary. These checks do not verify authentication, security, persistence, agent behavior, AWS, scalability, or production readiness. The Phase 0 evidence below remains historical and is not rewritten as current implementation status.

## Phase 0B historical checks — 2026-09-29

These checks were performed when the scaffold was implemented before explicit approval. A subsequent read-only audit identified the scope overreach; the user later accepted the scaffold as Phase 0B. The results below are historical evidence, not proof of original authorization or production readiness.

Verified locally on Windows on 2026-09-29 with Python 3.12.0, Node.js 24.21.0, and npm 10.8.0.

### Observed results

- Frontend dependency installation completed and generated `package-lock.json`.
- `npm run build` from `frontend/` completed successfully.
- Backend dependencies were installed in `backend/.venv`; `pip check` reported no broken requirements.
- `GET http://127.0.0.1:8000/api/health` returned `status: ok` and `service: novamind-api`.
- The same request through `http://127.0.0.1:5173/api/health` returned the expected response.
- The browser displayed the welcome page and the successful local API status after selecting **Check local API**.
- After stopping FastAPI, the browser displayed the unavailable-API message and allowed retry.
- After restarting FastAPI, selecting the same button restored the successful status.

These are local smoke checks, not a business-workflow test suite. No agent, Bedrock model, DynamoDB table, S3 bucket, authorization workflow, or deployed AWS environment was tested.

Follow [local development](local-development.md) to reproduce the checks.

## Phase 0C verification — completed 2026-10-01

The checks below used the existing Phase 0B dependencies. Previously completed build and health checks were retained across the continuation; they were not repeated merely to produce a new result.

### Application checks

- Existing environment: `backend/.venv/Scripts/python.exe -B -m pip check` passed with no broken requirements. FastAPI, Uvicorn, and Pydantic imports succeeded.
- Existing frontend: `npm.cmd run build` from `frontend/` passed, transforming 15 modules and producing the static bundle. npm offline mode was enabled, with audit, funding, and update notifications disabled. No installation command was run.
- Backend startup used the existing Python environment with `-B -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000`.
- Frontend startup used the installed Vite executable through `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort` from `frontend/`.
- Direct `GET http://127.0.0.1:8000/api/health` and proxied `GET http://127.0.0.1:5173/api/health` both returned `{"status":"ok","service":"novamind-api"}`. Both fields were checked for both responses.
- The browser rendered the welcome page. Selecting **Check local API** displayed: "The local API is responding. AWS and agent capabilities are not connected."
- Failure/recovery checks remain the historical Phase 0B evidence above; they were not rerun in Phase 0C.

### Consistency and scope checks

- All 17 Markdown files passed relative-link and trailing-whitespace checks. No live links remain to the old architecture, learning, or infrastructure paths.
- SHA-256 checks confirmed that both backend Python source files, frontend HTML/JSX/CSS/Vite configuration, all four dependency definition/snapshot/lock files, and both runtime selection files were unchanged from the start of Phase 0C.
- `infra/README.md` moved to `infrastructure/README.md`; its directory contained only that planning document and had no implementation references.
- The old architecture document moved into `docs/architecture/target-v2.md`, preserving its unique content; a separate current-baseline document was added.
- The old learning document moved into `learning/00-Project-Overview.md`, retaining its educational distinctions and increment template while adding the requested overview and ten questions.
- Tests, evaluations, scripts, and workflow directories contain purpose documents only. No test framework, executable script, evaluation implementation, or CI workflow was added.
- Final review found 32 project files outside generated directories, all untracked on `master`, with zero commits. No staging or commit was performed.
- Verification server processes were no longer running at completion.

No application verification failed. After the interrupted session, its old terminal process identifier was unavailable; only the unfinished documentation consistency check was rerun and passed.

No packages were installed or updated, no package registry was queried, and no external application service or AWS operation was called in Phase 0C. Application verification traffic used local loopback only. There is still no agent, Strands implementation, Bedrock integration, AWS SDK integration, business tool, or customer-resolution business logic. Production readiness, security, scalability, and AWS deployment remain unverified.
