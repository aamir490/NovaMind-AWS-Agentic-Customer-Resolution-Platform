# Phase 3: Local case review UI

The frontend now reviews existing support cases using only GET requests to Phase 1/2 APIs. No backend models, rules, fixtures, or endpoints were changed. This screen cannot create records, update case status, reserve stock, authorize actions, or execute refunds.

## Run and review

1. Start the existing backend and Vite servers using [local development](local-development.md).
2. Prepare synthetic customers/orders/cases through the existing [Phase 1 walkthrough](phase-1-domain.md). This setup is separate from the read-only UI. Include `LAP-1` (five units) or `LAP-2` (zero units) to use current policy/inventory fixtures.
3. Open `http://127.0.0.1:5173`, select **Refresh cases**, choose a case, then **Load case**.
4. Review the case description/status, customer, order ID, and ordered item quantities. Select an item to retrieve inventory and its backend-selected policy.
5. Enter requested quantity, days since delivery, return reason, and condition. Select **Check eligibility**.
6. Read the backend result, denial reasons, echoed assessment inputs, policy version, return prerequisite, and authorization flag.

Days/reason/condition require an explicit selection; they are not derived from case text. The UI labels them as unverified assumptions. The result is conditional eligibility, not entitlement or approval. Day 30 versus day 31 is a useful demonstration of the existing backend policy. Stock remains informational for returns/refunds.

## Implementation boundaries

`frontend/src/api.js` centralizes GET calls, timeouts, URL encoding, and readable errors. It retrieves the policy ID from inventory and passes eligibility inputs to the backend without evaluating rules. `CaseReview.jsx` handles display/selection and renders backend booleans and reasons. Denial-code translations are presentation text only; unknown codes remain visible.

Input constraints reflect the API input contract, not eligibility rules. For example, the UI allows days beyond the return window so the backend can return its denial reasons. Changing case, item, or assessment inputs clears old results. Controls are disabled while a review request is running so results cannot be attached to a different selection.

Empty lists explain how to prepare local data. Failed requests display an alert rather than inventing context or zero stock. For an item lookup retry, choose another item (or the blank option) and select the item again. **Refresh cases** reloads the list and clears the current review. Restarting the backend clears all records.

## Verification

```powershell
node --test tests/frontend-api.test.mjs
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

The frontend tests use Node's built-in test runner and mocked transport; no new packages are required. They check GET-only context loading, backend-selected policies, unchanged backend decisions, encoded inputs, missing inventory, empty lists, network errors, and API validation messages. Manual browser verification covers the rendered case-review flow and clearing old results. See [verification evidence](verification.md).

## Limitations and next step

Local single-worker memory only; no authentication, persistence, pagination, evidence verification, or action execution. Inventory and policy are demonstration fixtures. No automatic refresh or record creation exists in this UI. Full browser checks are recorded manual checks, not an installed browser-test framework.

Suggested Phase 4, subject to approval: define and implement a local proposal/review record with an explicit approval boundary and tests, still without executing financial actions or adding AI/AWS. Phase 4 has not begun.
