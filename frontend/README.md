# Frontend

The accepted Phase 0B React/Vite foundation now includes the Phase 3 read-only local case-review screen and the existing API health check. It fetches case/customer/order context, inventory, policy, and conditional eligibility using GET only. Business rules remain in the backend. See [the UI walkthrough](../docs/phase-3-case-review.md).

Use Node.js 24 and the existing `package-lock.json`. The initial commit has not been created. Phase 0C uses the installed dependencies without `npm ci` or other installation commands. See [local development](../docs/local-development.md) for the two-terminal workflow and separately labelled setup instructions.

During development, Vite forwards `/api` requests to `http://127.0.0.1:8000`. This keeps browser requests on one origin without introducing a permissive CORS policy. `npm run preview` previews static assets only and does not provide the development API proxy.

Phase 17 adds a Node.js 24 build stage and non-root Nginx runtime with same-origin `/api/` forwarding to the Compose backend. API errors and bearer headers are preserved; retries are disabled. The existing UI and its lack of login/bearer plumbing are unchanged, so protected requests still receive 401. The offline frontend build passed; the Docker/Nginx runtime is unverified because the local engine is unavailable. See [Phase 17 packaging, local startup, and limitations](../docs/phase-17-containers.md). No cloud deployment or Phase 18 work is included.
