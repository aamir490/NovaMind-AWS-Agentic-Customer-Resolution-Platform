# Phase 13 — Local Authentication & Authorization

Implementation and **27/27 focused Phase 13 tests passed**, built on the verified and pushed Phase 12 baseline. Full regression, frontend build, independent acceptance, and commit/push completion are not claimed for these changes. These are historical Phase 13 results. [Phase 14](phase-14-guardrails.md) now adds local AI safeguards while preserving the security boundaries described here; explicit injection patterns can be rejected before reaching a model.

## Contracts and provider

`backend/app/security.py` supplies frozen Pydantic contracts with unknown fields forbidden and instance revalidation enabled:

- `AuthenticatedIdentity`: stable UUID `user_id`, exactly one `CUSTOMER`, `REVIEWER`, or `ADMIN` role, and a required `customer_id` UUID only for CUSTOMER. REVIEWER/ADMIN identities cannot carry a customer binding.
- `LocalCredential`: a redacted `SecretStr` bearer token and its validated identity. Tokens must be 16–256 non-whitespace ASCII characters. This validates shape, not entropy; provision unpredictable tokens for local use.
- `ReviewInput`: nonblank `note` of at most 4000 characters. `reviewer_name`, role, and user ID are not accepted request fields.
- `SecurityError`: fixed 401/403 status and safe messages/codes without provider details.

`AuthenticationProvider.authenticate(token)` returns an identity or no match. The deterministic local implementation uses an explicit server-configured token registry, retains SHA-256 token digests, rejects duplicate tokens and conflicting definitions of the same user, and has no default credentials. Provider output is revalidated; errors or malformed output fail closed.

There is no login/token-issuance endpoint, password database, OAuth, social login, cloud provider, or authentication package. `create_app(auth_provider=...)` accepts a replacement provider. The default `create_app()` uses an empty registry: business endpoints are inaccessible until the trusted host configures credentials. Tokens are opaque credentials; a caller cannot supply role/customer/user claims inside a token or request and have them adopted as identity.

## Configuration and local use

Configure credentials in trusted application bootstrap code, outside requests and model input. The CUSTOMER binding must refer to a real case-service customer; administrative seeding/provisioning remains a trusted host operation. An illustrative configuration using host-supplied values is:

```python
from backend.app.main import create_app
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated,
)

provider = LocalAuthenticationProvider((
    LocalCredential(token=customer_token, identity=AuthenticatedIdentity(
        user_id=customer_user_id, role="CUSTOMER", customer_id=existing_customer_id)),
    LocalCredential(token=reviewer_token, identity=AuthenticatedIdentity(
        user_id=reviewer_user_id, role="REVIEWER")),
    LocalCredential(token=admin_token, identity=AuthenticatedIdentity(
        user_id=admin_user_id, role="ADMIN")),
))
app = create_app(auth_provider=provider)
# Provision domain records in app.state.case_service through trusted bootstrap or
# authenticated ADMIN HTTP calls before using customer-bound resources.

# Existing programmatic agents, memory service, tools, and HITL use this boundary:
with authenticated(provider, customer_token):
    paused = workflow.start(request)
with authenticated(provider, reviewer_token):
    result = workflow.resume({
        "workflow_id": paused.review.workflow_id,
        "case_id": paused.review.case_id,
        "proposal_id": paused.review.proposal_id,
        "review_id": paused.review.review_id,
        "decision": "REJECT", "note": "Reviewed evidence",
    })
```

HTTP clients send `Authorization: Bearer <configured-token>`. No token belongs in a URL, model message, tool argument, conversation record, or RAG document. Host code can replace the provider; no request can register credentials or choose a provider. Provision records and stable customer bindings deliberately: `create_app()` still creates a fresh domain store.

## Permissions and endpoint protection

All `/api/` business routes require authentication before routing/body validation. Public `/api/health` retains its existing process-health response. Documentation/OpenAPI pages remain public; they expose schemas, not records, and the middleware does not add an interactive bearer security scheme to OpenAPI. Use a header-capable API client. No frontend login or credential-management UI was added.

- **CUSTOMER:** read their own customer, orders, cases, and proposals; list only their own cases; create cases against their own orders; create pending proposals for their own cases; assess eligibility for their own orders/customer ID; access their own conversations. They cannot create customer/order records, change case status, review proposals, resume human decisions, or inspect workflow audit diagnostics.
- **REVIEWER:** read customer/order/case/proposal records across customers for review; create cases/pending proposals; change case status through existing permitted transitions; approve/reject proposals; resume HITL decisions and inspect audit diagnostics. They cannot create customer/order records. Reviewer assignment scoping is not implemented.
- **ADMIN:** broader operational access, including customer/order creation, with the same deterministic rules, case/order relationships, proposal transitions, HITL bindings, step/proposal limits, and no-execution boundary.
- Authenticated roles may read inventory/policy fixtures and use reference retrieval. None gains approval/execution tools; the tool catalog is unchanged.

Protected existing routes include customer/order creation and lookup, case creation/list/lookup/status updates, inventory/policy reads, eligibility checks, proposal creation/lookup/case listing, and proposal approve/reject. Agent, conversation, and HITL HTTP endpoints do not exist and were not added; their existing Python entry points now enforce the relevant context/permissions.

The existing frontend has no bearer credential plumbing. Protected business requests from that UI receive 401 until an authenticated client is used; the public health check still works. No anonymous compatibility fallback is provided.

## Ownership and isolation

Customer IDs in URLs, queries, bodies, and tool inputs are resource selectors, never identity claims. `Authorization` compares them to the authenticated CUSTOMER binding. Order/case ownership is read from live domain records; proposal ownership follows its live case. Case creation checks both the submitted customer selector and referenced order; the existing domain service still checks their relationship for all roles.

An unfiltered CUSTOMER case list is scoped server-side. Supplying another customer's filter does not widen it. Tools repeat ownership validation after their normal schema checks and before service invocation, covering customer/order/case/proposal lookups, eligibility, and pending proposal creation. A model request for another customer's record returns `FORBIDDEN` without the record.

Conversation service create/load/list/append checks authenticated ownership of the live case. SQLite's independent conversation/case binding validation remains intact. For CUSTOMER callers, unknown conversation IDs and wrong case bindings both become a safe access denial. Supplying one's own case ID with another conversation ID cannot load it, append to it, or send it to an LLM. The raw store remains a trusted persistence adapter without identity columns; it is not an HTTP or model interface.

## Authenticated review and HITL

Approve/reject HTTP inputs accept a note only. `reviewer_name` is derived as the canonical authenticated user UUID string in the existing proposal field. This retains a stable actor identifier without changing the proposal's deterministic transition logic. Caller-supplied reviewer names are rejected as unknown fields (422), not silently accepted.

`HumanDecision` retains workflow/case/proposal/review IDs, decision, and optional note; it no longer contains `reviewer_name`. A fresh authenticated REVIEWER or ADMIN context is required on every `resume`, before checkpoint lookup/mutation. The human-review node checks the role again after interrupt resumes, then derives the reviewer UUID from context. Mismatched IDs, invalid decisions, repeated reviews, and replay remain rejected. A CUSTOMER with all matching review IDs still cannot approve or reject.

HITL start requires authentication and still preflights case access through the protected tool path. Audit diagnostics require REVIEWER/ADMIN. No principal/token/role is checkpointed to authorize later operations. The human-review audit records `reviewer_user_id` as historical actor evidence, and the proposal retains the derived reviewer string; those recorded values are never used as authentication credentials or authority. Review still executes no action and invokes no subsequent LLM call.

## Identity propagation and trust boundaries

`authenticated(provider, token)` establishes a validated identity in a `ContextVar` and restores the previous context on exit. HTTP middleware spans the request with that scope; FastAPI's synchronous handler context and LangGraph's execution context propagate it. No process-global current-user assignment is used. Concurrent requests and explicit per-thread authenticated review scopes are tested for isolation.

Both agents obtain authorization through `LocalTools` and the conversation service. Identity is not a field of `AgentRequest`, graph state, memory contracts, RAG evidence, or LLM decisions. There is no principal injection into prompts and no token/role serialization into checkpoints. Background/thread callers must establish their own authenticated scope; possessing an old checkpoint does not restore an authenticated session.

Authentication context remains separate from conversation memory, workflow/checkpoint state, business records, and model content. Existing customer IDs in business/tool records and reviewer IDs in review/audit evidence are data; reading or quoting them cannot create an authenticated context. Stored conversation text and retrieved documents remain untrusted context, never authoritative business state. Injection may influence suggestions within permitted interfaces, but cannot alter the host's identity, add tools, become a reviewer, override eligibility, or execute actions.

Domain services and raw persistence adapters remain trusted internal building blocks. Direct Python access can call them or configure a provider; this is an application boundary, not a sandbox against hostile host code. Only the protected routes/tools/conversation/HITL interfaces should be exposed to untrusted callers.

## Failure behavior

- **401:** missing/malformed/duplicate Authorization headers, invalid tokens, provider failure/invalid identity, or programmatic access without context. HTTP returns `{"detail":"Authentication required"}` with `WWW-Authenticate: Bearer`.
- **403:** authenticated but wrong role, customer ownership mismatch, or customer access to unknown/non-owned resources. HTTP returns `{"detail":"Access denied"}` without IDs, records, tokens, or provider details.
- Existing **422** contract validation and **409** business conflicts remain; operational REVIEWER/ADMIN missing-record lookups retain **404**. CUSTOMER unknown and non-owned protected lookups use the same 403 to avoid confirming another customer's resource existence.
- Tools return safe `UNAUTHENTICATED`/`FORBIDDEN` envelopes. Agent preflight failures invoke no LLM. Programmatic conversation/HITL denials raise `SecurityError`; graph memory preflight translates it into a safe agent error. Persistence-write failures still do not retry business/review operations.

## Focused verification

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_security.py -v
```

**27/27 passed**. Tests use deterministic local credentials, in-process HTTPX ASGI transport (no socket server/network), scripted LLM providers, and temporary SQLite databases. Coverage includes all protected endpoints, default-deny startup, identity/credential validation, role permissions, customer ownership and list scoping, conversation/case/proposal isolation, safe failures, spoofed reviewer fields, prompt/memory/RAG privilege attacks, context isolation, fresh HITL authentication, strict checkpoint serialization, concurrent reviews, replay/binding controls, unchanged eligibility, RAG failure handling, and step/action limits.

Earlier regression fixtures were adapted to supply explicit ADMIN test credentials and use server-derived reviewer IDs. Assertions for deterministic behavior remain. Only `test_security.py` was executed in this phase; the adapted earlier suites have not been rerun. The Phase 12 **169/169** regression and frontend verification are historical baseline evidence, not results for these changes. No live Gemini/network/AWS calls, full regression, frontend tests/build, or package installation ran. `git diff --check` passed during implementation.

## Files and retained limits

- Backend: added `backend/app/security.py`; updated `backend/app/main.py`, `routes.py`, `operation_routes.py`, `proposal_routes.py`, `tools.py`, `conversations.py`, `agent.py`, `graph_agent.py`, and `hitl.py`.
- Tests: added `tests/test_security.py` and `tests/security_fixtures.py`; adapted `tests/test_agent.py`, `test_tools.py`, `test_api.py`, `test_proposals.py`, `test_hitl.py`, `test_conversations.py`, and `test_knowledge.py` for authenticated access/reviewer contracts.
- Documentation: `README.md`, `backend/README.md`, `tests/README.md`, `docs/PROJECT-PHASE-ROADMAP.md`, `docs/PROJECT-PHASE-ROADMAP-TABULAR.md`, `docs/phase-4-proposals.md`, `docs/phase-10-hitl.md`, `docs/phase-11-knowledge.md`, `docs/phase-12-conversations.md`, and the new `docs/phase-13-security.md`.

No dependency changes: existing Pydantic/FastAPI and Python standard library implement the feature; focused HTTP tests use the already installed HTTPX dependency. No cloud authentication, Cognito, OAuth/social login, frontend login UI, payment/refund execution, or unrelated infrastructure was added. The local provider has no expiry, persistent user directory, MFA, automatic token issuance/rotation, per-reviewer assignment, or rate limiting. A bearer proves possession of a configured local credential, not independently verified real-world identity. Protect tokens at the host; transport/storage encryption was not added. This is not a production authentication deployment.

SQLite still persists conversation memory only. Business/domain records, LangGraph/HITL checkpoints, and audit history remain in memory. A full application restart cannot continue a conversation until the authoritative case exists again, and cannot resume old HITL checkpoints. No cross-store transactions or automatic replay were added. No frontend memory UI or storage encryption exists. Stored text remains untrusted. Phase 14 was not part of this historical implementation; see the [current guardrails guide](phase-14-guardrails.md).
