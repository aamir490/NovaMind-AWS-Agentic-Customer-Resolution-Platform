# Evaluations

Phase 15 supplies a deterministic, offline evaluation harness over the existing Phase 1–14 components. It uses scripted model outputs, real local services/tools, the existing knowledge corpus, isolated conversation databases, and authenticated HITL decisions. It measures application behavior under fixed inputs; it does not measure a live model's intelligence.

`cases.py` holds the versioned `phase15-v1` scenarios and explicit expected outcomes. `phase15.py` supplies the fixtures, runner, rubric, metrics, offline guard, and JSON report. The application does not import this package. No dependency changes, remote provider, live service, or Phase 16 observability is added.

From the repository root, run the focused tests:

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_phase15_evaluations.py -v
```

**15/15 focused tests and 54/54 evaluation scenarios passed.** The tests also validate metric arithmetic, deliberately incorrect outcomes, empty/duplicate results, repeatability, loop/graph parity, offline enforcement, and CLI report/exit behavior. No previous test suite is imported or executed.

For a standalone JSON report (stdout only; no report files are written):

```powershell
./backend/.venv/Scripts/python.exe -B -m evaluations.phase15
```

The CLI exits 0 only when every scenario/check passes, otherwise 1. Reports contain stable scenario IDs, per-check actual/expected values and booleans, category pass counts/denominators/rates, and scenario totals. Runtime UUIDs, timestamps, credentials, raw prompts, and exception messages are excluded. Exceptions remain failed rows, never skipped successes. Retrieval scores include precision, document recall, reciprocal rank, and explicit no-match abstention.

See [Phase 15 rubrics, thresholds, results, and limitations](../docs/phase-15-evaluations.md). Change scenario expectations only after reviewing the intended behavior; do not update them just to make a failing run pass. Run the focused suite after changing the dataset, runner, or rubric.
