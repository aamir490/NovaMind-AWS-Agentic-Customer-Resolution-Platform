# Software tests

Phase 17 focused verification: `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_containers.py -v` — **14/14 passed**. Both Compose configurations validate and `npm.cmd --offline run build` passed using installed frontend dependencies. Startup/authentication contracts use in-process ASGI; health/smoke transports are mocked; packaging checks are static. Docker's Linux engine is unavailable, so no image-build or live-container success is claimed. No earlier suite/full regression/cloud call ran. See [commands, scope, and limitations](../docs/phase-17-containers.md). Phase 18 has NOT started. Results below are historical evidence.

Phase 16 focused verification: `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_observability.py -v` — **18/18 passed**. Tests cover traces, context isolation, safe metadata, reviewer-only diagnostics, retention, latency/errors, known/unknown token usage, mocked SDK usage, audit history, preserved safety, and sink failures without business retries. Scripted providers, temporary stores, and in-process ASGI only; socket/DNS calls are blocked. No earlier suites/evaluations, full regression, or frontend build ran. See [details and limitations](../docs/phase-16-observability.md).

Phase 15 focused verification: `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_phase15_evaluations.py -v` — **15/15 passed**, including **54/54 deterministic evaluation scenarios**, repeated-run equality, loop/graph parity, and negative tests for the evaluator. The suite uses scripted providers, fresh local stores, temporary SQLite/corpus files, and blocked network I/O. No earlier suite was imported or run. See [checks, metrics, and limits](../docs/phase-15-evaluations.md).

Phase 14 focused verification: `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_guardrails.py -v` — **22/22 passed**. Only this suite ran during Phase 14, using scripted providers, temporary local SQLite/corpus files, and blocked socket connections. It covers AI input/output limits, injection defenses, tool restrictions, policy, identity/ownership, and preserved HITL controls. See [results and limitations](../docs/phase-14-guardrails.md).

Phase 13 focused verification: `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_security.py -v` — **27/27 passed**. Tests use deterministic local authentication, in-process ASGI transport without network, scripted LLMs, and temporary SQLite files. Earlier regression fixtures now provide explicit ADMIN credentials and use authenticated reviewer IDs; those suites were not rerun. No full regression, frontend tests/build, or live services ran for Phase 13. See [coverage and limitations](../docs/phase-13-security.md).

Phase 12 is complete following independent verification reported by the user: focused conversation/persistence tests **25/25 passed**, full backend regression **169/169 passed**, frontend API tests **6/6 passed**, frontend production build **passed**, and `git diff --check` **passed with CRLF/LF normalization warnings only**. Those results were recorded during the Phase 12 documentation-only update and remain the baseline for Phase 13.

The focused command is `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_conversations.py -v`. This suite uses temporary isolated SQLite files and scripted LLM providers; it covers durable conversation memory and its Phase 8/9/10/11 safety boundaries. See [verification and preserved limitations](../docs/phase-12-conversations.md). Counts below are historical phase evidence.

Phase 1 uses Python's built-in `unittest`. `test_domain.py` checks validation, relationships, transition rules, immutable records, and isolated application stores. `test_api.py` exercises real HTTP on an ephemeral loopback port, with no external service or additional package.

Phase 2 adds `test_operations.py` for policy boundaries, quantities, condition/reason rules, missing fixtures, repeatability, and no mutation. The HTTP suite also covers read-only business lookups, eligibility, and error responses. There are 25 tests in total.

Phase 3 adds six frontend API tests using the built-in Node runner: `node --test tests/frontend-api.test.mjs`. These mock transport, not business rules. Rendered UI behavior is checked separately in the browser; see [verification evidence](../docs/verification.md). Total automated tests: 25 Python plus 6 Node.

From the repository root:

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

Authorization, business action retries, and agent evaluations remain future work. See [verification evidence](../docs/verification.md) for observed results and [the Phase 1 guide](../docs/phase-1-domain.md) for local API examples.
