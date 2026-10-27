# 🚀 NovaMind Agentic Customer Resolution Platform

## Engineering & Production Roadmap

> A production-oriented Agentic AI customer-resolution platform built step-by-step from deterministic business foundations to controlled AI agents, Amazon Bedrock, LangGraph, RAG, human-in-the-loop workflows, and production AWS deployment.

---

## 📌 Current Project Status

**Current Phase:** 🔄 Phase 17 — Containerization (configuration implemented; 14/14 focused tests, Compose validation, and offline frontend build passed; Docker runtime unverified)

**Completed:** ✅ Phase 0A through Phase 5. Phase 6 implementation and focused tests are complete and committed; broader verification is not claimed here.

**Next planned:** Phase 17A — Production Agentic Frontend & Live Workflow UX. Roadmap only; implementation is NOT started. Phase 18 — AWS Infrastructure Foundation is NOT started and outside the authorized scope. Bedrock remains deferred. Phase 17 packages the unchanged Phase 1–16 application; Docker engine unavailable, so no image-build/running-container verification, AWS deployment, full regression, or commit/push completion is claimed. See [Phase 17 setup and limitations](phase-17-containers.md).

Phase 8 completion is recorded at the user's direction: focused agent tests **15/15**, backend regression **84/84**, frontend API tests **6/6**, production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. This documentation update makes no new claim about commit or push status.

### Status Legend

| Symbol | Meaning |
|:---:|---|
| ✅ | Completed, verified, committed, and pushed |
| 🔄 | Current development phase |
| ⏳ | Planned / not started |
| 🚫 | Blocked |

---

# 🗺️ Development Journey

```text
Project Foundation
        ↓
Application Domain
        ↓
Deterministic Business Rules
        ↓
Case Review UI
        ↓
Human Approval Boundary
        ↓
Controlled Tool Layer
        ↓
LLM Foundation
        ↓
Amazon Bedrock
        ↓
AI Agent
        ↓
LangGraph Workflow
        ↓
Human-in-the-Loop
        ↓
RAG / Knowledge
        ↓
Persistence & Memory
        ↓
Authentication & Authorization
        ↓
Guardrails
        ↓
AI Evaluation
        ↓
Observability
        ↓
Containers
        ↓
Production Agentic Frontend & Live Workflow UX (17A)
        ↓
AWS Infrastructure
        ↓
AWS Deployment
        ↓
Production Data
        ↓
CI/CD & IaC
        ↓
Security Hardening
        ↓
Reliability & Cost
        ↓
Production Validation
        ↓
Portfolio Documentation
        ↓
Interview Mastery
```

---

# 📊 Complete Phase Roadmap

| Phase | Phase Name | Status |
|---:|---|:---:|
| 0A | Project Planning & Scope | ✅ |
| 0B | Local Application Foundation | ✅ |
| 0C | Repository Normalization & Reviewable Baseline | ✅ |
| 1 | Application Domain Foundation | ✅ |
| 2 | Deterministic Business Operations | ✅ |
| 3 | Local Case Review UI | ✅ |
| 4 | Resolution Proposal & Human Approval Boundary | ✅ |
| 5 | Agent-Ready Tool Layer | ✅ |
| 6 | LLM Foundation | Implemented; focused tests passed; committed |
| 7 | Amazon Bedrock Integration | ⏳ |
| **7A** | **Gemini LLM Integration** | **🔄 Implemented; final local verification passed** |
| 8 | Customer Resolution Agent | ✅ Complete; final verification passed |
| 9 | LangGraph Workflow | Verified baseline per user; retained unchanged |
| 10 | Human-in-the-Loop Agent Workflow | ✅ Complete; independent verification passed |
| 11 | Knowledge Base & RAG | ✅ Complete; independent verification passed |
| 12 | Conversation Memory & Persistence | ✅ Complete; independent verification passed |
| 13 | Authentication & Authorization | 🔄 Local implementation; 27 focused tests passed |
| 14 | Guardrails & AI Safety Controls | 🔄 Local implementation; 22 focused tests passed |
| 15 | AI Evaluation & Testing | 🔄 Local implementation; 15 focused tests and 54 scenarios passed |
| 16 | Observability & Auditability | 🔄 Local implementation; 18 focused tests passed |
| 17 | Containerization | 🔄 Configuration implemented; 14 tests/Compose/build passed; runtime unverified |
| 17A | Production Agentic Frontend & Live Workflow UX | ⏳ Planned only; implementation not started |
| 18 | AWS Infrastructure Foundation | ⏳ |
| 19 | AWS Application Deployment | ⏳ |
| 20 | Production Data Layer | ⏳ |
| 21 | CI/CD & Infrastructure as Code | ⏳ |
| 22 | Security & Governance Hardening | ⏳ |
| 23 | Reliability, Scaling & Cost Optimization | ⏳ |
| 24 | Production Readiness & End-to-End Testing | ⏳ |
| 25 | Portfolio & Architecture Documentation | ⏳ |
| 26 | Interview Mastery & Project Defense | ⏳ |

---

# 🏗️ FOUNDATION

## Phase 0A — Project Planning & Scope ✅

**Goal:** Define the business problem, project boundaries, development strategy, and target architecture.

### Outcomes

- Defined the customer-resolution use case
- Established project scope
- Defined architecture direction
- Established production-oriented development approach
- Established human approval as an important safety boundary
- AI functionality intentionally postponed until deterministic foundations exist

---

## Phase 0B — Local Application Foundation ✅

**Goal:** Create the smallest working local application foundation.

### Implemented

- React frontend
- Vite development/build system
- FastAPI backend
- Health/API connectivity
- Local project structure
- Basic frontend/backend integration

### Boundary

No:

- AI agents
- LLMs
- LangGraph
- RAG
- AWS integration
- customer-resolution business logic

---

## Phase 0C — Repository Normalization & Reviewable Baseline ✅

**Goal:** Establish a clean, documented, reviewable engineering baseline.

### Outcomes

- Repository structure normalized
- Documentation aligned with implementation
- Scope history documented
- Verification baseline established
- Git repository established
- Initial baseline committed and pushed

---

# 🧩 BUSINESS APPLICATION FOUNDATION

## Phase 1 — Application Domain Foundation ✅

**Goal:** Build the core customer-support domain before introducing AI.

### Implemented

- Customer model
- Order model
- Support Case model
- Case status lifecycle
- Validated request/response models
- Customer APIs
- Order APIs
- Case APIs
- Server-managed IDs
- Server-managed timestamps
- Controlled status transitions

### Current Storage

```text
Local / In-Memory
```

---

## Phase 2 — Deterministic Business Operations ✅

**Goal:** Implement important business decisions as explicit, testable rules.

### Implemented

- Inventory lookup
- Return/refund policy lookup
- Eligibility assessment
- Customer ownership validation
- Purchased quantity validation
- Return-window validation
- Return-reason validation
- Product-condition validation
- Explicit denial reasons

### Core Design Principle

```text
Deterministic Business Rules
            ≠
       LLM Reasoning
```

Rules that can be reliably implemented in code should remain deterministic.

### Safety Boundary

```text
Eligibility
    ≠
Authorization
    ≠
Execution
```

An eligible request does not automatically authorize or execute a refund.

---

## Phase 3 — Local Case Review UI ✅

**Goal:** Give a human reviewer a readable interface for reviewing customer cases.

### Implemented

- Existing case review
- Customer information
- Order information
- Inventory information
- Policy information
- Eligibility result
- Assumptions
- Denial reasons
- Centralized frontend API calls

### Architecture Principle

```text
Frontend
   ↓
Backend APIs
   ↓
Business Services
```

Business eligibility logic remains on the backend.

The frontend does not independently recreate business rules.

---

# 👤 HUMAN CONTROL BOUNDARY

## Phase 4 — Resolution Proposal & Human Approval Boundary ✅

**Goal:** Separate a proposed customer resolution from human authorization.

### Workflow

```text
Support Case
     ↓
Resolution Proposal
     ↓
PENDING_REVIEW
     ↓
Human Review
   ↙        ↘
APPROVED    REJECTED
```

### Implemented

- Resolution proposal model
- Proposal linked to support case
- Proposed resolution/action
- Rationale
- Server-managed proposal ID
- Server-managed timestamps
- `PENDING_REVIEW`
- `APPROVED`
- `REJECTED`
- Explicit human approval
- Explicit human rejection
- Invalid transition protection
- Repeated review protection
- Concurrent conflicting review protection

### Critical Safety Boundary

```text
APPROVED
   ≠
EXECUTED
```

Approval currently does **not**:

- issue refunds
- process payments
- modify inventory
- modify orders
- execute financial transactions
- trigger external systems

### Verification

```text
Focused Phase 4 Tests: PASSED
Full Backend Regression: 36 / 36 PASSED
```

Phase 4 was committed and pushed to GitHub.

---

# 🧰 AGENT FOUNDATION

## Phase 5 — Agent-Ready Tool Layer ✅

**Status:** Approved, verified, committed, and pushed.

**Goal:** Create a controlled application capability layer that future AI agents can safely use.

### Implemented Tools

- Customer lookup
- Order lookup
- Support-case lookup
- Inventory lookup
- Policy lookup
- Eligibility assessment
- Resolution proposal creation
- Proposal status lookup

### Target Architecture

```text
Future AI Agent
       ↓
Controlled Tool Layer
       ↓
Existing Application Services
       ↓
Deterministic Business Rules
       ↓
Business Data
```

### Design Requirements

- Reuse existing services
- Do not duplicate business logic
- Validate tool inputs
- Return structured outputs
- Keep business rules deterministic
- Keep sensitive operations outside agent control

### Agent Tools Must NOT

- approve proposals
- reject proposals
- execute refunds
- execute payments
- modify financial records
- bypass eligibility rules
- bypass human approval

### Not Included Yet

- LLM
- Amazon Bedrock
- LangGraph
- RAG
- AWS deployment
- Authentication
- Database
- Autonomous agent

---

# 🧠 LLM FOUNDATION

## Phase 6 — LLM Foundation — implemented

The provider-neutral interface, validated JSON responses, explicit failures, and fake provider are implemented and committed. Ten focused tests passed. The original planning list below is historical; Phase 6 does not implement transport timeouts or a real provider. See [Phase 6 contracts](phase-6-llm.md).

**Goal:** Introduce a clean LLM abstraction without creating an unrestricted AI agent.

### Planned Work

- Model abstraction/interface
- Prompt boundaries
- Structured model input
- Structured model output
- Output validation
- Error handling
- Timeout handling
- Configuration management

### Target Architecture

```text
Application
     ↓
LLM Abstraction
     ↓
Model Provider
```

---

## Phase 7 — Amazon Bedrock Integration ⏳

Deferred: no Bedrock work is authorized by Phase 7A.

**Goal:** Connect the LLM layer to Amazon Bedrock.

### Planned Work

- Amazon Bedrock integration
- AWS SDK interaction
- Model configuration
- Structured responses
- Error handling
- Timeout handling
- IAM permissions
- Environment configuration

### Design Principle

```text
Application Logic
      ↓
LLM Abstraction
      ↓
Amazon Bedrock
```

Application business logic should not become tightly coupled to a single model.

---

## Phase 7A — Gemini LLM Integration 🔄

The isolated Google GenAI adapter implements the Phase 6 interface with environment credentials, configurable model, JSON schema output, HTTP timeout, and explicit failure mapping. Eleven mocked tests passed. The user reported final verification: full backend regression **69/69 passed**, frontend API tests **6/6 passed**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. Live API verification remains unverified; this update does not claim commit or push completion. No agent or tool calling was added. See [Phase 7A details](phase-7a-gemini.md).

---

# 🤖 AGENTIC AI

## Phase 8 — Customer Resolution Agent ✅ Complete

Implemented: validated request/result contracts, bounded structured LLM/tool loop, existing Phase 5 allowlist, per-run audit history, one case-bound pending proposal per run, and application-rendered informational/human-review outcomes. Final verification reported by the user: **15/15 focused agent tests**, **84/84 backend regression tests**, **6/6 frontend API tests**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. No live Gemini verification is claimed. See [contracts, usage, and limitations](phase-8-agent.md). Phase 8 is complete and retained unchanged as the Phase 9 behavioral baseline.

**Goal:** Build the first AI agent capable of reasoning about customer-support cases through controlled tools.

### Planned Capabilities

The agent may:

- understand a customer request
- inspect customer information
- inspect order information
- inspect case information
- retrieve policy information
- check eligibility
- select allowed tools
- reason about possible resolutions
- create a resolution proposal

### Critical Boundary

```text
Agent
  ↓
Proposes Resolution

Human
  ↓
Authorizes Sensitive Decision
```

The agent must not independently authorize sensitive business actions.

---

## Phase 9 — LangGraph Workflow 🔄

**Status:** Verified baseline per the user's Phase 10 instruction; **25/25 focused fake-provider tests** remain the recorded implementation-time evidence. Retained unchanged in Phase 10; no additional regression counts are inferred.

Six explicit nodes handle case loading, reasoning, validated tool execution, audit recording, finalization, and safe failure. State is Pydantic-validated. Existing tools, providers, business rules, and the Phase 8 baseline remain unchanged. LangGraph **1.2.12** is the only new direct dependency. No checkpointing, persistence, or HITL pause/resume was added. See [Phase 9 contracts and verification](phase-9-langgraph.md). The target workflow below is planning context; human review integration is deferred to Phase 10.

**Goal:** Move agent execution into an explicit stateful workflow.

### Target Workflow

```text
START
  ↓
Understand Request
  ↓
Gather Context
  ↓
Retrieve Policy
  ↓
Assess Eligibility
  ↓
Reason About Resolution
  ↓
Create Proposal
  ↓
Human Review
  ↓
END
```

### Planned Concepts

- Graph state
- Nodes
- Edges
- Conditional routing
- Tool nodes
- Error paths
- Workflow state
- Checkpoints

---

## Phase 10 — Human-in-the-Loop Agent Workflow ✅ Complete

**Status:** Complete following user-reported independent verification: focused HITL tests **20/20 passed**, full backend regression **129/129 passed**, frontend API tests **6/6 passed**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. No new commit/push completion is claimed.

Limitations remain: reviewer identity is not authenticated; checkpoints are in-memory and lost on restart; no durable recovery or frontend HITL UI exists; approval/rejection does not execute business actions.

The local LangGraph workflow interrupts after pending proposal creation, stores JSON-compatible state in `InMemorySaver`, and resumes with a validated workflow/case/proposal/review-bound human decision. It calls the existing Phase 4 review service directly, records audit history, and ends without another LLM call or business action. No new packages, durable storage, authentication, or frontend UI were added in Phase 10. See [Phase 10 contracts and limitations](phase-10-hitl.md). These boundaries remain authoritative in Phase 11.

**Goal:** Integrate human review directly into the agentic workflow.

### Target Flow

```text
Customer
   ↓
AI Agent
   ↓
Controlled Tools
   ↓
Resolution Proposal
   ↓
Workflow Pause
   ↓
Human Review
  ↙     ↘
Approve  Reject
   ↓
Controlled Continuation
```

### Planned Capabilities

- Pause workflow for human review
- Preserve workflow state
- Resume after decision
- Record reviewer decision
- Handle rejection
- Handle alternative resolution paths

---

# 📚 KNOWLEDGE & RAG

## Phase 11 — Knowledge Base & RAG ✅ Complete

**Status:** Complete following user-reported independent verification: focused Knowledge/RAG tests **15/15 passed**, full backend regression **144/144 passed**, frontend API tests **6/6 passed**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. No new commit/push completion is claimed.

Limitations remain: local lexical/token-cosine retrieval only; no production semantic embedding provider/vector database, live embedding/network calls, or frontend RAG UI. Retrieved content remains untrusted information; deterministic business rules and HITL remain authoritative.

Three support guidance/SOP fixtures, validated source/document/chunk/result contracts, deterministic loading/chunking, embedding/retrieval interfaces, and lexical cosine search are available through read-only `search_knowledge`. Existing LangGraph/HITL paths consume the new tool through their catalog without orchestration changes. Retrieved text is untrusted reference data; no domain, eligibility, policy, inventory, proposal, or human-review rule changes. No new packages, neural embeddings, vector database, network calls, or production persistence. See [Phase 11 details](phase-11-knowledge.md). The target concepts below remain planning context where they exceed this local baseline.

**Goal:** Ground AI responses in trusted customer-support knowledge.

### Potential Knowledge Sources

- Return policies
- Refund policies
- Product information
- Support procedures
- FAQ documents
- Internal support guidance

### Target RAG Flow

```text
Customer Request
       ↓
Query
       ↓
Retrieval
       ↓
Relevant Knowledge
       ↓
LLM Context
       ↓
Grounded Response
```

### Planned Concepts

- Document ingestion
- Chunking
- Embeddings
- Vector search
- Retrieval
- Context construction
- Source references/citations
- Retrieval evaluation

---

# 💾 MEMORY & PERSISTENCE

## Phase 12 — Conversation Memory & Persistence ✅ Complete

**Status:** Complete following user-reported independent verification: Phase 12 focused conversation/persistence tests **25/25 passed**, full backend regression **169/169 passed**, frontend API tests **6/6 passed**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. These results were recorded without rerunning tests or builds during this documentation-only update. No new commit/push completion is claimed. See [contracts, configuration, verification, and limitations](phase-12-conversations.md).

**Goal:** Persist local conversation text while keeping memory, checkpoints, business state, and audit history separate.

### Implemented scope

- Validated immutable conversation/message contracts and a replaceable create/load/list/append store interface.
- Standard-library SQLite storage, stable case-bound conversation UUIDs, server timestamps, and transactional sequence ordering.
- Optional memory integration for Phase 8/9 agents and Phase 10 HITL; stored text is bounded UNTRUSTED CONTEXT.
- Default history limits: latest 20 messages within a 16000-character serialized envelope.
- Safe failures for missing/invalid/corrupt data and cross-case access; no automatic business-operation replay.
- Temporary-database tests cover recreation, isolation, ordering, RAG/HITL preservation, and malicious memory.

### Important Architecture Separation

```text
Authoritative Business State
        ≠
Conversation Memory
        ≠
Agent Workflow State
        ≠
Audit History
```

SQLite persists conversation memory only. Business/domain records, LangGraph/HITL checkpoints, and audit history remain in memory. Full application restart cannot resume a conversation until authoritative case state exists again. There are no cross-store transactions, automatic replay, encryption, or frontend memory UI. Stored conversation text remains untrusted context, not authoritative business state. Phase 12 added no authentication; Phase 13's local authentication is described below. No dependencies or cloud infrastructure were added in either phase.

---

# 🔐 IDENTITY & ACCESS

## Phase 13 — Authentication & Authorization 🔄

**Status:** Local implementation and **27/27 focused security tests passed**. Full regression, frontend build, independent acceptance, and commit/push are not claimed. See [contracts, local configuration, permissions, verification, and limitations](phase-13-security.md).

**Goal:** Introduce replaceable local authentication and server-side authorization without cloud identity infrastructure.

### Implemented

- Validated authenticated identities and deterministic server-configured bearer credentials; empty/default registry denies access.
- CUSTOMER ownership checks, REVIEWER human operations, and broader ADMIN operations subject to existing deterministic rules.
- Authentication on business HTTP routes; protected tool, conversation, and HITL interfaces; safe 401/403 responses.
- Server-derived reviewer UUIDs; caller-supplied reviewer names are rejected.
- Request-scoped identity outside prompts, conversation memory, RAG, and checkpoint authority; audit actor IDs are evidence only.
- In-process ASGI, scripted-model, and temporary-database tests for role/ownership isolation, spoofing, prompt/memory/RAG attacks, and retained HITL safety.

No dependencies, cloud authentication, OAuth/social login, frontend login UI, or action execution were added. The existing frontend cannot access protected records without bearer credentials. Static local tokens have no expiry or automated lifecycle; this is not production authentication. Conversation-only SQLite persistence and the in-memory domain/checkpoint/audit limits remain. Phase 14 adds the local safeguards below while retaining these boundaries.

---

# 🛡️ AI SAFETY

## Phase 14 — Guardrails & AI Safety Controls 🔄

**Status:** Local implementation and **22/22 focused tests passed**. `git diff --check` passed with CRLF/LF normalization warnings only. No full regression, frontend build, live network/Gemini/AWS calls, or commit/push is claimed.

**Goal:** Reduce unsafe, invalid, or unauthorized AI behavior.

### Implemented Controls

- Input validation
- Output validation
- Tool allowlists
- Tool argument validation
- Prompt-injection defenses
- Sensitive-action restrictions
- Policy enforcement
- Model failure handling
- Unexpected tool-call handling

Local checks now screen agent input, selected memory, tool arguments, and tool results for explicit injection patterns; bound payloads and model context; and reject ambiguous/invalid model JSON and invalid tool output. Existing authentication, ownership, deterministic policy, tool allowlists, case/proposal/step limits, authenticated HITL, and no-execution boundaries remain authoritative. HITL reuses the guarded graph nodes. No new dependency or live provider/service call is required. See [Phase 14 implementation, focused verification, failure codes, and limitations](phase-14-guardrails.md). Broader regression, production safety, and commit/push completion are not claimed. Phase 15 evaluates these retained boundaries below.

### Core Principle

```text
LLM Suggestion
      ≠
Business Authorization
```

---

# 🧪 AI EVALUATION

## Phase 15 — AI Evaluation & Testing 🔄

**Goal:** Measure AI-system behavior instead of relying only on demonstrations.

**Status:** **15/15 focused tests and 54/54 local evaluation scenarios passed.** Existing application code/dependencies are unchanged. No broader regression, frontend build, live Gemini/network/AWS call, or commit/push is claimed.

### Implemented Local Evaluation Areas

- Response quality
- Policy correctness
- Tool selection
- Tool arguments
- Retrieval quality
- Groundedness
- Hallucination behavior
- Workflow completion
- Human escalation
- Failure handling
- Regression evaluation

The versioned local dataset drives both agent implementations, real deterministic policies/tools, lexical RAG, Phase 14 guardrails, and authenticated HITL with scripted model responses. Reports include per-check actual/expected values, category pass counts/rates, retrieval precision/recall/reciprocal rank, and explicit overall pass/fail. Focused tests verify repeatability, loop/graph parity, offline enforcement, and that incorrect outcomes fail the grader. Response quality/groundedness checks concern application templates and evidence integrity; no live model quality or semantic hallucination score is claimed. See [Phase 15 approach, rubrics, results, and limitations](phase-15-evaluations.md). Phase 16 adds passive local diagnostics below; these evaluation results remain historical evidence.

---

# 📈 OBSERVABILITY

## Phase 16 — Observability & Auditability 🔄

**Goal:** Make application and agent behavior observable and diagnosable.

**Status:** Local implementation; **18/18 focused tests passed**. No dependency change, full regression, frontend build, live network/Gemini/AWS call, or commit/push completion is claimed. `git diff --check` passed with line-ending warnings only.

### Implemented Local Capabilities

- Structured application logs
- Request tracing
- Agent execution tracing
- Tool-call logging
- Error metrics
- Latency metrics
- Model usage
- Token usage
- Audit events
- Human-review history

Bounded metadata-only events cover HTTP authentication/routing, both agents, tools, structured model calls, eligibility, proposals, and HITL. Server-generated trace/span IDs correlate nested operations; error/latency/model/token metrics distinguish known usage from unknown counts. Successful review events derive actor IDs from authenticated context. Diagnostics require REVIEWER/ADMIN. Telemetry failures never retry or authorize business operations. Existing audit history, business rules, security, and no-execution boundaries remain authoritative. See [Phase 16 configuration, verification, and limitations](phase-16-observability.md). Remote exports, durable audit storage, production monitoring, and per-model cost accounting are not implemented. Phase 17 packages these existing components below.

### Target

```text
User Request
     ↓
Trace ID
     ↓
API
     ↓
Agent
     ↓
Tool Calls
     ↓
LLM Calls
     ↓
Business Operations
     ↓
Human Decision
     ↓
Audit Trail
```

---

# 🐳 DEPLOYMENT FOUNDATION

## Phase 17 — Containerization 🔄

**Goal:** Prepare application components for repeatable production deployment.

**Status:** Container configuration implemented; **14/14 focused tests, base/optional-auth Compose validation, and offline frontend build passed**. Docker's Linux engine is unavailable; image builds, Nginx runtime validation, container health/startup, and live smoke checks remain unverified. No dependency changes, full regression, AWS deployment, or commit/push completion is claimed.

### Implemented Configuration

- Backend Dockerfile
- Frontend deployment/build strategy
- Container configuration
- Environment configuration
- Health checks
- Local container verification
- Image optimization

Backend/frontend multistage Dockerfiles, non-root single-worker runtime, same-origin API proxy, narrow context allowlists, read-only Compose filesystems, an internal application network plus a frontend-only edge network, loopback publication, process health checks, optional mounted credential configuration, and a read-only smoke checker are provided. Dependency/build caches and frontend build tools stay outside final images. Authentication still defaults to an empty registry, and no business state is persisted or seeded. Image sizes, immutable digest reproducibility, and production readiness are not verified. See [Phase 17 setup, checks, and limitations](phase-17-containers.md). **Phase 18 is NOT started.**

---

## Phase 17A — Production Agentic Frontend & Live Workflow UX ⏳

**Status:** Proposed scope only. This update changes the two roadmap files; no UI, API, tests, dependencies, or deployment are implemented. Phase 17 verification limitations remain unchanged. Phase 18/AWS is NOT started.

**Goal:** Replace the Phase 3 demo presentation with a professional dark enterprise workspace over the existing Phase 4–17 components. “Production” describes the intended UX quality, not a claim of production authentication, durable workflow recovery, or deployment readiness.

### Planned Frontend Scope

- **Enterprise shell:** restrained dark surfaces, consistent typography/spacing, accessible contrast, keyboard navigation, visible focus, responsive navigation and panels, and reduced-motion support. Provide honest loading, empty, denied, unavailable, stale, and error states.
- **Authentication:** credential entry for the existing local bearer provider, backend-confirmed identity/role, protected navigation, and client sign-out that clears credentials and case caches. Keep credentials in memory, never in URLs, logs, bundles, or persistent browser storage. Do not invent password login, registration, token expiry/refresh, or server revocation.
- **Dashboard:** authorized case queue with search/filtering, case status, pending reviews, and backend-confirmed workflow summaries. Label count scope and freshness; show unavailable metrics as unavailable, never synthetic activity, savings, or success rates.
- **Case workspace:** case/customer/order context, inventory, authoritative policy and eligibility results, proposals, and a review panel. Preserve Phase 3 read capabilities and display backend decisions without reproducing eligibility rules in JavaScript.
- **AI assistant and conversation history:** case-bound requests and validated application responses, ordered saved messages, clear run/conversation IDs, input limits, and explicit provider/guardrail/memory failures. Distinguish conversation text from authoritative case, proposal, and workflow state; prevent duplicate submissions and do not replay writes after an uncertain response.
- **Live agent/LangGraph tracking:** a readable graph and timeline using the actual nodes (`load_case`, `reason`, `execute_tool`, `record_result`, `finalize`, `fail_safely`, plus HITL `prepare_review`/`human_review`). Display only backend-observed transitions, repeated steps, pauses, completion, and failures; expose safe operational summaries, never private model reasoning or raw prompts.
- **Tool activity and RAG evidence:** server-reported allowed-tool calls, safe result summaries and errors, and retrieved document/title/version/chunk references, excerpts, method, and scores where available. A retrieval score is not model confidence. Clearly label evidence as untrusted reference material; absent evidence stays absent.
- **Proposals and HITL:** show proposed action, rationale, authoritative eligibility, pending/approved/rejected status, and an explicit reviewer approve/reject form. Workflow reviews must use the bound workflow/case/proposal/review IDs and authenticated reviewer identity. Reconcile stale, repeated, and conflicting decisions with the backend; always state that approval does not execute an action.
- **Audit and observability:** REVIEWER/ADMIN views for permitted workflow/review history, trace correlation, latency/errors, known/unknown token usage, and event-retention gaps. Separate operational telemetry from authoritative business review state; hide privileged diagnostics from CUSTOMER views.

### Real Live State Contract

- Backend state/events are the sole source of workflow progress. No timed node animations, invented percentages, fake tool calls, simulated typing, or replaying a completed trace as a live run. Ordinary loading indicators express a pending request only.
- Plan a bounded, authenticated run snapshot and incremental event contract: server-generated run/workflow and case IDs, ordered event IDs/sequences, server timestamps, actual node/tool lifecycle state, safe outcomes/errors, proposal/review linkage, and trace correlation where available. These are proposed contracts, not existing HTTP APIs.
- Prefer authenticated polling of snapshots/events as the initial transport; evaluate SSE only if needed. Any stream must preserve bearer authorization without query-string credentials. Reads must work while execution is active; a synchronous final response followed by client animation does not satisfy live tracking.
- On reconnect, deduplicate/order events and reconcile with the authoritative snapshot. Bound retention and pagination; report dropped events, stale connections, unknown runs, and restart loss explicitly. A disconnected browser neither implies failure nor authorizes starting/resuming the workflow again.
- Preserve existing workflow outcomes (`REVIEW_REQUIRED`, `REVIEWED`, `COMPLETED`, `FAILED`) and proposal statuses. Running/resuming/node activity must be explicitly reported by the backend. Transport state, workflow outcome, human decision, and action execution are separate concepts.

### Existing APIs and Required Integration Gaps

Repository baseline: `backend/app/main.py` registers only case/customer/order, business-operation, proposal/review, and health routes. `frontend/src/api.js` currently offers unauthenticated GET helpers. The following gaps must be resolved as narrowly scoped Phase 17A integration work when implementation is authorized; endpoint names and schemas still need definition.

1. **Identity:** bearer validation and role/ownership checks exist, but no current-user/permissions endpoint or frontend auth client exists. Add a minimal authenticated identity contract over the existing provider. Static credentials have no expiry/refresh/revocation service; production identity infrastructure remains out of scope.
2. **Dashboard:** `GET /api/cases` provides an ownership-filtered case list; case detail/context and per-case proposal APIs exist. There is no aggregate dashboard, workflow list, or paginated queue contract. Reuse authorized reads first, label derived counts accurately, and add only bounded summaries/pagination required by the workspace.
3. **Agent execution and live tracking:** `GraphResolutionAgent` and `HITLWorkflow` are local Python components, not wired into FastAPI. Their public execution methods return after execution/pause; the graph stream is consumed internally, HITL uses synchronous invocation/locking, and no HTTP start/status/events endpoint exists. Plan trusted server-side composition, bounded execution scheduling, run ownership, request deduplication, and safe incremental state/event publication without changing graph decisions, budgets, tools, or review semantics. Carry authenticated and observing context into execution; never accept identity, provider configuration, graph state, or checkpoint data from the browser.
4. **Tool/RAG presentation:** tool histories and `search_knowledge` retrieval metadata exist internally, but there is no safe run-scoped HTTP projection. Add allowlisted, access-controlled summaries/evidence tied to the actual run. Do not serialize raw graph state, unrestricted tool payloads, prompts, secrets, or full internal audit objects to the browser; do not open a generic tool-execution API.
5. **Conversation history:** `ConversationService` and the SQLite store support case-bound create/list/load/append locally, but are not composed into the HTTP app and have no routes. Plan authorized history/read and user-message submission contracts that reuse those services; only the server may record assistant outputs. Preserve limits and independent memory-write failure handling without replaying proposals/reviews.
6. **HITL:** proposal GET/list and POST approve/reject routes already exist, but no paused-workflow lookup/resume endpoint exists. A direct proposal approval does not resume a graph. Add a bound decision/resume adapter over `HITLWorkflow.resume`; select the correct path for workflow-bound versus standalone proposals, never call both for one decision. Preserve authenticated reviewer identity and mismatch/already-reviewed/conflict protection.
7. **Audit/observability:** HTTP responses already supply `X-Trace-ID`; `HITLWorkflow.audit` and `LocalObserver.snapshot` are local REVIEWER/ADMIN-only methods. Add bounded, sanitized read contracts for permitted diagnostics and metrics, preserving role restrictions, retention-gap reporting, metadata-only telemetry, and unknown usage values. Diagnostics must never become an authorization or execution mechanism.
8. **Runtime wiring:** the container backend has no external egress and no agent/provider/memory composition today. Plan local deterministic provider-backed integration through the real workflow; clearly label that provider as local/scripted. “Live” means real execution events, not a live Gemini call. An unavailable provider must produce an unavailable UI state. Keep same-origin `/api`, internal backend networking, frontend-only edge networking, loopback publication, and container hardening intact; no network relaxation or new persistence deployment is implied.

### Preserved Boundaries and Exclusions

- Preserve all Phase 4–17 security and business behavior: eligibility is not authorization; approval is not execution. No refunds, payments, order/inventory mutations, external actions, autonomous approval/rejection, or model-selected privileged operations.
- Enforce existing CUSTOMER ownership and REVIEWER/ADMIN permissions server-side for every new read, start, event subscription/poll, history request, and decision. UI visibility is not authorization. Guardrails, tool allowlists, structured validation, case/step/proposal limits, and human-review checks remain authoritative.
- Treat assistant text, conversation history, and RAG excerpts as untrusted display content; render safely, without executable HTML or unsafe citation links. Keep model/provider secrets on the backend and preserve telemetry redaction.
- SQLite remains conversation-only; domain records, workflow checkpoints, and audit/telemetry remain in memory. Do not claim durable workflow recovery, cross-store transactions, multi-worker coordination, automatic replay, or complete audit retention.
- No unrelated refactoring or unnecessary dependencies; no changes to the three untracked JSON files. No Phase 18/AWS, Bedrock integration, cloud authentication, production data layer, remote telemetry export, or live Gemini/network/AWS verification. Broader production infrastructure remains in its existing later phases.

### Planned Acceptance and Focused Verification

- Implement only after separate authorization, starting with minimal contracts and backend adapters, then auth/shell/case views, then assistant/history/live tracking/evidence, then HITL/diagnostics. Reuse existing React/Vite, FastAPI, LangGraph, and Phase 15 deterministic evaluation fixtures where suitable.
- Future focused local tests must drive the real backend workflow with scripted providers and real controlled services; separately test browser rendering with recorded contract fixtures clearly confined to tests. Check that UI transitions follow backend events, including slow/no-event runs, tool/RAG failures, review pauses, and terminal outcomes.
- Verify unauthorized/cross-customer access, role isolation, safe rendering/redaction, empty/unavailable states, event ordering/duplication/gaps/reconnect, restart loss, uncertain submissions without replay, mismatched/stale/concurrent HITL decisions, and memory failures after committed business changes.
- Check keyboard/contrast/responsive behavior, preservation of existing case-review capabilities and container `/api` routing, and that approval/rejection executes no business action. Run focused Phase 17A tests only when implemented, without live provider/network/AWS calls. This roadmap-only update runs no application tests and claims no implementation or new verification results.

---

# ☁️ AWS FOUNDATION

## Phase 18 — AWS Infrastructure Foundation ⏳

**Goal:** Build the secure AWS infrastructure required by the application.

### Potential AWS Areas

- Amazon VPC
- Public/private networking
- Security Groups
- IAM
- Secrets Manager
- CloudWatch
- Amazon ECR
- Load balancing
- Application networking

### Principle

```text
Least Privilege
+
Explicit Network Boundaries
+
Secure Configuration
```

---

## Phase 19 — AWS Application Deployment ⏳

**Goal:** Deploy the working application to AWS.

### Potential Architecture

```text
User
 ↓
Amazon CloudFront
 ↓
Frontend
 ↓
Application Load Balancer
 ↓
Application Services
 ↓
Business / Data / AI Services
 ↓
Amazon Bedrock
```

### Potential AWS Services

- Amazon S3
- Amazon CloudFront
- Amazon ECR
- Amazon ECS Fargate
- Application Load Balancer
- Amazon Bedrock
- AWS Secrets Manager
- Amazon CloudWatch
- IAM
- VPC

Final AWS services will be selected based on actual architecture requirements.

---

# 🗄️ PRODUCTION DATA

## Phase 20 — Production Data Layer ⏳

**Goal:** Move appropriate application state to durable production storage.

### Planned Concerns

- Durable business records
- Workflow persistence
- Conversation history
- Cache/session strategy
- Backup strategy
- Recovery strategy
- Data lifecycle
- Data access patterns

### Principle

Storage technology should be selected based on actual access patterns and requirements.

---

# 🔄 CI/CD & IaC

## Phase 21 — CI/CD & Infrastructure as Code ⏳

**Goal:** Automate testing, builds, infrastructure changes, and deployment.

### Planned Pipeline

```text
Developer
    ↓
GitHub
    ↓
Automated Tests
    ↓
Build
    ↓
Container Image
    ↓
Amazon ECR
    ↓
Infrastructure / Deployment
    ↓
AWS Environment
    ↓
Verification
```

### Potential Technologies

- GitHub Actions
- Docker
- Amazon ECR
- Terraform / CloudFormation / CDK as appropriate
- AWS deployment services

---

# 🔒 SECURITY & GOVERNANCE

## Phase 22 — Security & Governance Hardening ⏳

**Goal:** Review and strengthen the complete platform from a production-security perspective.

### Planned Areas

- IAM least privilege
- Secrets management
- Encryption
- Network boundaries
- API protection
- Data protection
- Dependency security
- Audit logging
- Sensitive-action controls
- AI-specific security controls
- Governance requirements

---

# ⚡ RELIABILITY, SCALE & COST

## Phase 23 — Reliability, Scaling & Cost Optimization ⏳

**Goal:** Prepare the platform for realistic production operational concerns.

### Reliability

- Failure handling
- Retries
- Timeouts
- Graceful degradation
- Recovery strategy
- Dependency failure handling

### Scaling

- Stateless services where appropriate
- Horizontal scaling
- Load handling
- Data-layer scaling
- Model/API rate considerations

### Cost

- Amazon Bedrock usage
- Compute
- Storage
- Logging
- Network traffic
- Scaling policies
- Idle-resource cost

---

# ✅ PRODUCTION VALIDATION

## Phase 24 — Production Readiness & End-to-End Testing ⏳

**Goal:** Validate the complete system as one production-oriented workflow.

### Planned Test Scenarios

- Happy path
- Invalid customer
- Invalid order
- Invalid case
- Ineligible request
- Agent failure
- Tool failure
- LLM failure
- Retrieval failure
- Human rejection
- Unauthorized request
- Concurrent actions
- Service failure
- Timeout
- Recovery behavior

### Target End-to-End Flow

```text
Customer Request
       ↓
Authenticated Application
       ↓
Agentic Workflow
       ↓
LLM / Amazon Bedrock
       ↓
Controlled Tools
       ↓
Deterministic Business Rules
       ↓
RAG / Knowledge
       ↓
Resolution Proposal
       ↓
Human Approval
       ↓
Authorized Controlled Action
       ↓
Audit + Observability
```

---

# 📖 PORTFOLIO

## Phase 25 — Portfolio & Architecture Documentation ⏳

**Goal:** Turn the completed engineering work into a professional portfolio project.

### Planned Deliverables

- Professional README
- Current architecture diagram
- Production AWS architecture diagram
- End-to-end request flow
- Technology stack
- Agent architecture
- LangGraph workflow
- RAG architecture
- Security architecture
- Deployment explanation
- Design decisions
- Trade-offs
- Testing strategy
- Evaluation strategy
- Cost considerations
- Known limitations
- Future improvements

### Documentation Rule

```text
Claim only what was actually
implemented and verified.
```

---

# 🎯 INTERVIEW MASTERY

## Phase 26 — Interview Mastery & Project Defense ⏳

**Goal:** Be able to confidently explain and defend every important design decision in the project.

### Preparation

- 30-second project introduction
- 2-minute project explanation
- 5-minute architecture explanation
- End-to-end request flow
- Agentic AI explanation
- LangGraph explanation
- Tool-calling explanation
- RAG explanation
- Amazon Bedrock explanation
- AWS architecture explanation
- Security explanation
- Scaling explanation
- Cost explanation
- Failure scenarios
- Troubleshooting stories
- Design trade-offs
- Alternative architectures
- STAR stories
- Project-specific interview Q&A

### Learning Method

For every important technology or architecture decision:

```text
What is it?
     ↓
Why do we need it?
     ↓
How does it work?
     ↓
Where is it used in NovaMind?
     ↓
Why did we choose it?
     ↓
What alternatives exist?
     ↓
What can fail?
     ↓
How do we troubleshoot it?
     ↓
How would we improve it?
```

---

# 🎯 TARGET PRODUCTION ARCHITECTURE

```text
                         CUSTOMER
                            │
                            ▼
                    ┌──────────────┐
                    │   Frontend   │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │ Backend/API  │
                    └──────┬───────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Agentic Workflow │
                  │    LangGraph     │
                  └────────┬─────────┘
                           │
             ┌─────────────┴─────────────┐
             │                           │
             ▼                           ▼
      ┌──────────────┐            ┌──────────────┐
      │ LLM/Bedrock  │            │     RAG      │
      └──────┬───────┘            │  Knowledge   │
             │                    └──────────────┘
             ▼
      ┌────────────────┐
      │ Controlled     │
      │ Tool Layer     │
      └───────┬────────┘
              │
              ▼
      ┌──────────────────┐
      │ Deterministic    │
      │ Business Rules   │
      └────────┬─────────┘
               │
               ▼
      ┌──────────────────┐
      │ Resolution       │
      │ Proposal         │
      └────────┬─────────┘
               │
               ▼
      ┌──────────────────┐
      │ Human Review     │
      │ Approve / Reject │
      └────────┬─────────┘
               │
               ▼
      ┌──────────────────┐
      │ Authorized       │
      │ Controlled Action│
      └────────┬─────────┘
               │
               ▼
      ┌──────────────────┐
      │ Audit / Logs /   │
      │ Observability    │
      └──────────────────┘
```

---

# 🛡️ Core Engineering Principles

1. **Deterministic rules stay deterministic.**
2. **AI must not bypass business rules.**
3. **Agents interact through controlled tools.**
4. **Eligibility is not authorization.**
5. **Approval is not execution.**
6. **Sensitive operations require explicit authorization.**
7. **Tool inputs and outputs must be validated.**
8. **Business logic should not be duplicated across layers.**
9. **Human oversight remains available for sensitive decisions.**
10. **Security boundaries must be explicit.**
11. **Important operations must be observable and auditable.**
12. **AI behavior must be evaluated, not merely demonstrated.**
13. **AWS services are added only when justified.**
14. **Documentation must match actual implementation.**
15. **Production claims require implementation and verification.**

---

# 🔄 Development Workflow

```text
┌─────────────────────────────┐
│           ChatGPT           │
│ Architecture + Phase Scope  │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│            Codex            │
│ Focused Implementation      │
│ + Focused Tests             │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│      Kiro / PowerShell      │
│ Regression + Build + Git    │
│ Verification                │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│           ChatGPT           │
│ Architecture / Code Review  │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│            Git              │
│      Commit + Push          │
└──────────────┬──────────────┘
               │
               ▼
        NEXT PHASE
```

---

# ✅ Phase Definition of Done

A phase is marked **Completed** only when:

- [ ] Required implementation is complete
- [ ] Focused tests pass
- [ ] Regression verification passes
- [ ] Existing functionality remains intact
- [ ] Documentation matches implementation
- [ ] Phase scope boundaries were respected
- [ ] Git changes were reviewed
- [ ] Changes were committed
- [ ] Changes were pushed to GitHub

After completion:

```text
Current Phase

🔄 → ✅

Next Phase

⏳ → 🔄
```

---

# 📍 Current Position

```text
FOUNDATION
────────────────────────────────
Phase 0A  ✅
Phase 0B  ✅
Phase 0C  ✅

BUSINESS FOUNDATION
────────────────────────────────
Phase 1   ✅
Phase 2   ✅
Phase 3   ✅

HUMAN CONTROL
────────────────────────────────
Phase 4   ✅

AGENT FOUNDATION
────────────────────────────────
Phase 5   ✅

LLM & AGENTIC AI
────────────────────────────────
Phase 6   Implemented; focused tests passed; committed
Phase 7   ⏳ Bedrock deferred
Phase 7A  Gemini adapter; final local verification passed
Phase 8   ✅ Complete; final verification passed
Phase 9   Verified baseline; retained unchanged
Phase 10  ✅ Complete; independent verification passed

KNOWLEDGE & STATE
────────────────────────────────
Phase 11  ✅ Complete; independent verification passed
Phase 12  ✅ Complete; independent verification passed

SECURITY & AI QUALITY
────────────────────────────────
Phase 13  🔄 Local implementation; 27 focused tests passed; broader verification pending
Phase 14  🔄 Local guardrails implementation; 22 focused tests passed
Phase 15  🔄 Local evaluation; 15 focused tests and 54 scenarios passed
Phase 16  🔄 Local observability; 18 focused tests passed

CLOUD & PRODUCTION
────────────────────────────────
Phase 17  🔄 Configuration implemented; 14 tests/Compose/build passed; runtime unverified
Phase 17A ⏳ Planned frontend/live workflow UX only; implementation NOT started
Phase 18  ⏳ NOT started; outside authorized scope
Phase 19  ⏳
Phase 20  ⏳
Phase 21  ⏳
Phase 22  ⏳
Phase 23  ⏳
Phase 24  ⏳

PORTFOLIO & INTERVIEW
────────────────────────────────
Phase 25  ⏳
Phase 26  ⏳
```

---

# 👨‍💻 Project

**NovaMind Agentic Customer Resolution Platform**

**Built by Aamir**
