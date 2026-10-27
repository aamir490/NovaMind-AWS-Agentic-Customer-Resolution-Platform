# Current local baseline

Phase 8 adds a standalone in-process `ResolutionAgent` over `StructuredLLM` and `LocalTools`. It is bounded, records per-run history, and can only create pending proposals through existing services. It can be composed with the Gemini adapter but is not wired into HTTP routes or the frontend. Fifteen fake-provider tests passed; live agent behavior remains unverified. See [Phase 8](../phase-8-agent.md). The API/UI baseline below remains unchanged.

Status: Phase 5 local tool layer over Phase 4 proposal/review services, with the unchanged Phase 3 case-review UI, extending Phase 2 operations, the Phase 1 domain, and the accepted Phase 0B scaffold. This document describes only current behavior.

Phase 5 exposes `app.state.local_tools` for in-process Python calls. An explicit eight-tool allowlist validates inputs and delegates to the same case, business-operation, and proposal services used by the APIs. Proposal creation is the only write tool; human review and execution are excluded. Phase 5 itself added no agent, SDK adapter, or HTTP route. See [tool contracts and limits](../phase-5-tools.md).

Phase 4 adds an independent in-memory proposal service wired to the existing case store. It records proposed actions and allows one explicit approve/reject transition under a process-local lock. Reviewer labels are unverified; no authenticated authorization or financial execution exists. See [proposal contracts and limitations](../phase-4-proposals.md).

```text
Browser
    -> React/Vite
    -> local /api proxy
    -> FastAPI
    -> /api/health
```

Phase 1 also exposes `/api/customers`, `/api/orders`, and `/api/cases` routes through the same proxy. These routes call a local application service, validate customer/order relationships and case-status transitions, and store immutable records in process memory. See the [domain/API guide](../phase-1-domain.md) for contracts and limitations. No persistent repository or external provider is connected.

Vite serves the welcome page on `127.0.0.1:5173`. React renders a button and holds a small status value with `useState`. Clicking the button uses browser `fetch` to request `/api/health`, with a five-second timeout. The page validates the response and displays success or failure; it allows another attempt.

The Vite development server forwards `/api` to `http://127.0.0.1:8000`. Uvicorn serves the FastAPI application at `app.main:app`, using `backend/` as the application directory. `GET /api/health` returns:

```json
{"status":"ok","service":"novamind-api"}
```

The response confirms that the local API process responds. It does not check any downstream service. FastAPI also supplies its default local API documentation at `/docs`.

The frontend uses native fetch and React state; no router or separate state/API library was added. The backend uses FastAPI, Pydantic, and Uvicorn. Node 24 and Python 3.12 are the accepted local runtime selections. Exact dependency definitions and resolved versions remain in the existing frontend manifest/npm lock and backend requirements files.

`npm run build` produces static frontend files. `npm run preview` previews those files but does not provide the development API proxy. This is not a deployment architecture.

## Absent capabilities

- No agent connected to HTTP routes or the frontend.
- No Bedrock.
- No verified resolution authorization or action execution. Eligibility uses demonstration policy and unverified scenario inputs only.
- No database.
- No AWS deployment.

The frontend includes case/item selection and a read-only assessment-input form. It uses existing GET endpoints and renders backend results without eligibility-rule duplication. There are no record-editing or proposal-review controls in the UI. Human review remains an explicit backend API operation. No refund/replacement execution or authentication exists. Customer/order consistency checks do not authenticate the caller. The action lifecycle displayed on the page is explanatory text, not enforcement.

Phase 2 adds inventory and policy GET endpoints plus an order eligibility GET endpoint. The read-only service reuses customer/order records, reads immutable fixtures, and calls a pure rule evaluator. It performs no state mutation or external I/O. See [Phase 2 assumptions and contracts](../phase-2-business-operations.md).

See [local development](../local-development.md) for commands and [verification evidence](../verification.md) for checks actually performed. Security, scalability, and production readiness have not been verified. The [target V2 design](target-v2.md) is a separate plan.
