# Backend

Phase 1 retains `GET /api/health` and adds an in-memory customer/order/support-case API. Health checks only the responding process, not database, model, or AWS readiness. Interactive API documentation is available at `/docs`. See the [Phase 1 domain/API guide](../docs/phase-1-domain.md) for contracts, examples, status rules, and limitations.

See [local development](../docs/local-development.md) for installation, startup, and verification.

Phase 2 adds `operations.py` (immutable local fixtures, pure rules, and read-only service) and `operation_routes.py` (GET endpoints). It reuses Phase 1 customer/order lookup. See [business operations](../docs/phase-2-business-operations.md) for policy assumptions and the distinction between conditional eligibility and authorization.

## Future boundaries

Current modules under `app/` separate HTTP routes, domain rules/services, controlled tools/agents, and conversation persistence. `create_app()` still creates independent in-memory business stores; restart/reload discards business data and HITL checkpoints. Use one worker for this local baseline.

Phase 12 adds optional standard-library SQLite conversation storage through `ConversationService` and the agents' `memory=` parameter. Provision/open a local database explicitly; HTTP startup does not create one. Conversation text survives reopening, but missing cases/checkpoints are never restored from text. See [Phase 12 configuration, contracts, and limitations](../docs/phase-12-conversations.md).

Phase 13 adds local authentication and authorization. Configure `create_app(auth_provider=...)` with an explicit server-side credential registry; the default is empty and denies business API access. Clients send a bearer header. Tools, conversation services, and HITL callers use `authenticated(provider, token)`; models cannot supply this context. REVIEWER/ADMIN review inputs omit `reviewer_name`, which the server derives from the authenticated user UUID. See [Phase 13 roles, setup, endpoint protection, and limits](../docs/phase-13-security.md). The frontend has no login/credential UI, so its protected business calls receive 401. Health remains public. No new dependency was added.

Phase 14 adds deterministic local guardrails for agent input, selected memory, tool arguments/results, and structured model output. Existing callers need no configuration changes. Explicit injection patterns and excessive/ambiguous payloads fail safely; authentication, deterministic policy, tool restrictions, and HITL remain authoritative. No new dependencies or live services are required. See [Phase 14 limits, failure codes, and focused verification](../docs/phase-14-guardrails.md).

Phase 15 evaluates these existing components through the separate `evaluations` package without changing backend code or dependencies in that phase. **15/15 focused tests and 54/54 local scenarios passed** using scripted providers, isolated stores, and blocked network I/O. These are historical results; see [Phase 15 metrics and limits](../docs/phase-15-evaluations.md).

Phase 16 adds passive local observability around existing execution boundaries. `create_app()` owns a bounded observer and returns server-generated `X-Trace-ID` headers on handled responses. Programmatic callers bind `observing(observer)` separately from authentication; observer snapshots require REVIEWER/ADMIN. Optional local JSON logging, error/latency metrics, nullable provider token counts, and proposal/review metadata add no execution authority. **18/18 focused tests passed**, without dependencies or live services. See [setup and limits](../docs/phase-16-observability.md).

Phase 17 adds a Python 3.12 multistage Dockerfile and isolated `container/` bootstrap/health modules. The runtime is non-root, single-worker, and default-deny; optional credentials are loaded from a mounted file through existing security contracts. No business state or conversation database is seeded. Application modules and dependencies are unchanged. **14 focused container tests and Compose checks passed**; Docker image/runtime validation remains blocked by the unavailable engine. See [container configuration and limitations](../docs/phase-17-containers.md). Phase 18 has NOT started.

Keep business rules independent of FastAPI and Strands. Keep authorization outside model decisions. Review V1 components individually before reuse.

## Dependencies

`requirements.in` lists direct runtime dependencies. `requirements.txt` started as a `pip freeze` snapshot of the installed bootstrap environment and is not a hash-verified dependency lock. AI-02B adds pinned boto3 and its required transitive packages from published package metadata without installing or resolving the full environment; that dependency set still needs an isolated install and verification before deployment. Do not generate a snapshot from global Python packages.
