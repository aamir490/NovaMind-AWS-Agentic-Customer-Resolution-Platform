Current status: **Phase 17 container configuration implemented; 14/14 focused tests, Compose validation, and offline frontend build passed**. Docker's Linux engine is unavailable; image builds and running-container verification remain unverified. Existing Phase 1–16 application code/dependencies are unchanged. See [Phase 17 setup and limitations](phase-17-containers.md). No full regression, live model/AWS calls, deployment, or commit/push is claimed. Earlier counts remain historical evidence. **Phase 18 is NOT started.**

**Next planned: Phase 17A — Production Agentic Frontend & Live Workflow UX.** Planning only; no implementation started. This documentation update changes only the two roadmaps and makes no new test/build claim. Phase 18/AWS remains outside scope.

Local bearer authentication now establishes CUSTOMER/REVIEWER/ADMIN permissions and authenticated reviewer IDs; credentials default to an empty registry. SQLite still persists conversation memory only. Business/domain records, LangGraph/HITL checkpoints, and audit history remain in memory. Full application restart cannot resume a conversation until authoritative case state exists again. There are no cross-store transactions, automatic replay, encryption, frontend login UI, or frontend memory UI. Stored conversation text remains untrusted context, not authoritative business state.

| Phase | Name | Main Goal |
|---|---|---|
| **0A** | Project Planning & Scope | Define problem, boundaries and target architecture |
| **0B** | Local Application Foundation | React + FastAPI minimal application |
| **0C** | Repository Normalization & Baseline | Clean structure, documentation and verification |
| **1** | Application Domain Foundation | Customer, order, support case and statuses |
| **2** | Deterministic Business Operations | Inventory, policies and eligibility rules |
| **3** | Local Case Review UI | Human-readable support-case review interface |
| **4** | Resolution Proposal & Human Approval Boundary | Proposal → approve/reject without executing actions |
| **5** | Agent-Ready Tool Layer | Safe structured tools over deterministic services |
| **6** | LLM Foundation | Introduce the model layer with structured outputs |
| **7** | Amazon Bedrock Integration | Connect the application to AWS Bedrock models |
| **7A** | Gemini LLM Integration | Isolated provider adapter over Phase 6; final local verification passed; see [details](phase-7a-gemini.md) |
| **8** | First Customer-Resolution Agent — COMPLETE | Final verification passed; see [details](phase-8-agent.md) |
| **9** | LangGraph Workflow — VERIFIED BASELINE | Retained unchanged; [25 implementation-time focused tests](phase-9-langgraph.md) |
| **10** | Human-in-the-Loop Agent Workflow — COMPLETE | Independent verification passed; [results and limitations](phase-10-hitl.md) |
| **11** | Knowledge Base & RAG — COMPLETE | Independent verification passed; [results and limitations](phase-11-knowledge.md) |
| **12** | Conversation Memory & Persistence — COMPLETE | Independent verification passed; durable conversation memory only; [results and limitations](phase-12-conversations.md) |
| **13** | Authentication & Authorization — IMPLEMENTED | Local provider, roles, ownership checks, authenticated reviews; [27 focused tests and limitations](phase-13-security.md) |
| **14** | Guardrails & AI Safety Controls — IMPLEMENTED LOCALLY | Input/output bounds, injection checks, retained tool/action restrictions; [focused verification and limits](phase-14-guardrails.md) |
| **15** | AI Evaluation & Testing — IMPLEMENTED LOCALLY | Scripted local cases, explicit rubrics, retrieval metrics, safety/HITL and parity checks; [15 focused tests / 54 scenarios and limitations](phase-15-evaluations.md) |
| **16** | Observability & Auditability — IMPLEMENTED LOCALLY | Bounded safe logs/traces, error/latency/usage metrics, proposal/review audit; [18 focused tests and limits](phase-16-observability.md) |
| **17** | Containerization — CONFIGURATION IMPLEMENTED | Multistage images, local Compose, health/auth configuration; [14 focused tests and runtime limitations](phase-17-containers.md) |
| **17A** | Production Agentic Frontend & Live Workflow UX — PLANNED ONLY | Dark enterprise workspace, auth, dashboard/cases, assistant/history, real workflow/tool/RAG tracking, proposals/HITL and audit; API adapters required; implementation not started |
| **18** | AWS Infrastructure Foundation | VPC, IAM, networking and required AWS resources |
| **19** | AWS Application Deployment | Deploy frontend/backend/services to AWS |
| **20** | Production Data Layer | Durable database/cache/storage architecture |
| **21** | CI/CD & Infrastructure as Code | Automated testing, build and deployment |
| **22** | Security & Governance Hardening | Least privilege, secrets, encryption and controls |
| **23** | Reliability, Scaling & Cost Optimization | HA/scaling/recovery/cost controls |
| **24** | Production Readiness & End-to-End Testing | Full system validation and failure testing |
| **25** | Portfolio & Architecture Documentation | Professional README, diagrams and project story |
| **26** | Interview Mastery & Project Defense | Architecture explanation, Q&A and STAR stories |

## Phase 17A — Proposed Scope and API Gaps

The [detailed Phase 17A plan](PROJECT-PHASE-ROADMAP.md#phase-17a--production-agentic-frontend--live-workflow-ux-) defines the planned contracts, boundaries, and acceptance checks. “Production” is a UX quality target, not a production-readiness claim.

### Planned Experience

- Replace the Phase 3 demo with an accessible, responsive dark enterprise shell, consistent typography/navigation, keyboard/focus support, and honest loading/empty/error/stale/denied states.
- Add local bearer credential entry, backend-confirmed identity/roles, protected navigation, and client sign-out; memory-only credentials, no invented login/refresh/revocation service.
- Provide an authorized dashboard and case workspace with customer/order context, deterministic policy/eligibility, proposals, and explicit reviewer approve/reject controls.
- Add a case-bound assistant, saved conversation history, real LangGraph node/run status, tool activity, retrieved RAG sources/excerpts, and REVIEWER/ADMIN audit/observability views with trace IDs and known/unknown usage.

### Required Backend Integration (Not Implemented)

- **Auth/dashboard:** existing bearer enforcement and case/context/proposal reads can be reused. Current-user/permissions, aggregate workflow/dashboard summaries, and pagination contracts are missing; derive only accurately scoped counts from authorized records.
- **Agent/live status:** graph/HITL components run locally and synchronously; FastAPI has no agent start, run lookup, incremental events, or paused-workflow resume routes. Plan trusted composition, bounded execution scheduling, run ownership and deduplication, preserved authentication/observability context, and safe snapshots/events readable during execution.
- **Tools/RAG/history:** tool and evidence records exist internally; case-bound conversation services/SQLite are not wired into the HTTP app. Add bounded, authorized projections and user-message/history adapters, with server-only assistant messages. Do not expose raw graph state, unrestricted payloads, or a generic tool executor.
- **HITL:** proposal approve/reject APIs exist but do not resume LangGraph. Add a workflow/case/proposal/review-bound decision adapter over the existing resume service; route standalone and workflow reviews correctly without reviewing twice, replaying writes, or accepting client reviewer identities.
- **Audit/observability:** `X-Trace-ID` exists; audit/snapshot methods are local and privileged. Add sanitized, bounded REVIEWER/ADMIN read contracts; preserve redaction, retention-gap reporting, and unknown usage. Telemetry remains separate from authoritative decisions.
- **Runtime:** container composition does not wire agent/provider/memory services; backend networking blocks external egress. Use a clearly labeled local scripted provider through real workflows for deterministic integration. Do not relax networking for Gemini or imply new durable storage.

### Live-State and Safety Requirements

- Progress must reflect actual backend state/events, never timers, fake node/tool animations, invented percentages/typing, or playback presented as live execution. Pending-request indicators do not imply workflow progress.
- Plan authenticated snapshot/event polling first; SSE is optional only if needed and must preserve authorization without URL tokens. Use server run/case IDs, ordered event sequences, timestamps, actual node/tool outcomes and review linkage. Bound retention; reconcile reconnects, duplicates, stale state, event gaps, unknown runs and restart loss without repeating mutations.
- Preserve Phase 4–17 roles/ownership, deterministic rules, guardrails, controlled tools, structured outputs and budgets. Eligibility is not authorization; approval is not execution. No autonomous review, financial/external actions, or business-rule duplication in the UI. Render conversation/RAG/model content as untrusted data without exposing prompts, private reasoning, or secrets.
- Preserve conversation-only SQLite, in-memory domain/checkpoint/audit limits, same-origin `/api`, internal backend plus frontend-only edge networking, loopback publication, and container hardening. No durable recovery, cross-store transactions, multi-worker or production-auth claims.
- Future acceptance: focused local tests for real scripted-provider workflows and event-driven UI, access isolation, safe rendering, tool/RAG/guardrail failures, reconnect/order/gaps, no replay, conflicting/stale HITL decisions, memory failures, accessibility, and retained case/container behavior. No live Gemini/network/AWS calls; no tests run for this planning-only change.
- Implement only after separate authorization. No unrelated refactoring, unnecessary dependencies, edits to the three untracked JSON files, or Phase 18/AWS work.
