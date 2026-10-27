# Phase 4: Resolution proposals and human review boundary

**Historical contract note:** Phase 13 protects these HTTP endpoints with authentication and authorization. Review bodies now accept only `note`; the server derives the reviewer UUID from authenticated REVIEWER/ADMIN context and rejects caller-supplied `reviewer_name`. The older HTTP examples below record Phase 4 behavior. See [current calling contracts](phase-13-security.md). Domain transition and no-execution rules remain unchanged.

Local, in-memory API workflow only. No frontend changes or action execution are included.

## Proposal and review contract

A proposal links to an existing support case and records an `action` (`return`, `refund`, or `replacement`) and a nonblank `rationale` (up to 4000 characters). These are proposed resolution categories, not executable payment instructions. Case association, action, and rationale cannot be edited after creation.

The server assigns a UUID, UTC creation/update timestamps, and initial `PENDING_REVIEW` status. Review fields start as null. The only permitted transitions are:

```text
PENDING_REVIEW -> APPROVED
PENDING_REVIEW -> REJECTED
```

The explicit approve/reject requests require a nonblank `reviewer_name` (up to 120 characters) and `note` (up to 4000 characters). The server sets the terminal status, `reviewed_at`, and `updated_at`, retaining the original creation timestamp and proposed content. An in-process lock makes checking and recording the decision atomic. Repeated reviews, reversals, and attempts to reopen a reviewed proposal are rejected, including repeated requests for the same decision.

All request models reject extra fields. Callers cannot set proposal IDs, status, timestamps, or review metadata during creation, nor change proposed content through a review request. Records are immutable snapshots. Different proposals may reference the same case; reviews are scoped to one exact proposal ID.

## API

- `POST /api/proposals`: `{ "case_id": "<UUID>", "action": "refund", "rationale": "Request a human review." }` -> 201.
- `GET /api/proposals/{proposal_id}` -> 200.
- `GET /api/cases/{case_id}/proposals` -> 200, insertion-order list; `[]` for a known case without proposals.
- `POST /api/proposals/{proposal_id}/approve`: `{ "reviewer_name": "Local reviewer", "note": "Reviewed the proposed resolution." }` -> 200.
- `POST /api/proposals/{proposal_id}/reject`: same required review fields -> 200.

Missing case/proposal: 404. Already-reviewed proposal or invalid service-level review transition: 409. Invalid UUID, unsupported action, blank/oversized required fields, or client-supplied server fields: 422. There is no generic status-update, proposal-edit, or execution endpoint.

## Local example

Prepare `$case` using the existing [Phase 1 walkthrough](phase-1-domain.md), with the backend running locally:

```powershell
$api = 'http://127.0.0.1:8000/api'
$body = @{case_id=$case.id; action='refund'; rationale='Request manual review of the wrong-item complaint.'} | ConvertTo-Json
$proposal = Invoke-RestMethod -Method Post -Uri "$api/proposals" -ContentType 'application/json' -Body $body
Invoke-RestMethod "$api/cases/$($case.id)/proposals"
$review = @{reviewer_name='Local reviewer'; note='Reviewed this proposal; no financial action is executed.'} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$api/proposals/$($proposal.id)/approve" -ContentType 'application/json' -Body $review
```

Use `/reject` instead of `/approve` for a rejection. After either decision, another review returns 409 without altering the first review. The existing Phase 3 UI remains read-only and has no proposal/review controls.

## What this boundary does and does not guarantee

The application requires a separate review operation; proposal creation and eligibility checks never automatically approve anything. Reviewing a proposal changes only that proposal. It does not update the case, reserve inventory, send a notification, issue a refund, create a replacement, or perform any external call.

There is **no authentication**. The reviewer name is a caller-supplied label, not a verified human identity. Any local caller can invoke these endpoints; this is not a production authorization or access-control boundary. `APPROVED` records an explicit local review decision only, not permission for an external financial transaction. No LLM or eligibility result grants approval.

This phase does not bind proposals to verified evidence, payment details, quantities, amounts, or a policy assessment, and it does not enforce one proposal per case. Multiple proposals can receive separate decisions. Later execution would need its own authorized design, exact action parameters, identity checks, current policy/evidence validation, and duplicate-action protection. Nothing here executes or authorizes that later work.

Memory resets on restart/reload. Use one process/worker; locks do not coordinate separate workers. There is no durable audit trail, persistence, or public-service security. Use synthetic local data only.

## Focused verification

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_proposals.py -v
```

Eleven focused tests passed: seven proposal-service tests and four proposal HTTP tests. They cover server fields, missing records, immutable proposals, both review decisions, repeated/invalid transitions, concurrent review races, isolation/filtering, input protection, and unchanged case/order/inventory state. The HTTP tests use an ephemeral loopback server and stop it afterward.

No full regression, frontend build, or browser verification was run, as requested. No packages were installed. Phase 5 has not started.
