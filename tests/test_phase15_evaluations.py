"""Focused Phase 15 evaluation and grader validation; no prior suites are imported."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.app.agent import AgentResult
from backend.app.llm import LLMResponse
from evaluations.cases import AGENT_CASES, GUARDRAIL_CASES, HITL_CASES, RAG_CASES
from evaluations.phase15 import (
    Check, FINAL, Fixture, ScriptedProvider, agent_row, evaluate, guarded_row, main,
    offline, response_checks, retrieval_metrics, summarize,
)


class Phase15Evaluations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluate()

    def test_all_reviewed_scenarios_pass(self):
        failures = {r["id"]: [c for c in r["checks"] if not c["passed"]]
                    for r in self.report["results"] if not r["passed"]}
        self.assertEqual(failures, {})
        self.assertTrue(self.report["passed"])

    def test_dataset_coverage_and_denominators(self):
        expected = {f"{mode}/{case.name}" for mode in ("loop", "graph") for case in AGENT_CASES}
        expected.update(f"hitl/{name}" for name in HITL_CASES)
        expected.update(f"rag/{name}" for name, *_ in RAG_CASES)
        expected.update(f"guardrails/{name}" for name, *_ in GUARDRAIL_CASES)
        expected.add("harness/offline")
        self.assertEqual({r["id"] for r in self.report["results"]}, expected)
        self.assertEqual(len(expected), 54)
        self.assertEqual(self.report["scenarios"]["total"], len(expected))
        self.assertEqual(sum(m["total"] for m in self.report["metrics"].values()),
                         sum(len(r["checks"]) for r in self.report["results"]))
        self.assertTrue({"response_quality", "policy_correctness", "tool_selection", "tool_arguments",
                         "retrieval_quality", "groundedness", "hallucination_containment", "workflow_completion",
                         "human_escalation", "failure_handling", "guardrails", "hitl", "structured_outputs",
                         "safety_boundaries", "bounded_execution", "harness_integrity"} <= self.report["metrics"].keys())

    def test_report_is_repeatable_despite_runtime_ids_and_timestamps(self):
        self.assertEqual(evaluate(), self.report)

    def test_loop_and_graph_regression_parity(self):
        results = {r["id"]: r["checks"] for r in self.report["results"]}
        for spec in AGENT_CASES:
            with self.subTest(case=spec.name):
                self.assertEqual(results[f"loop/{spec.name}"], results[f"graph/{spec.name}"])

    def test_empty_duplicate_or_unscored_results_are_rejected(self):
        valid = {"id": "one", "checks": [Check("pass", "safety", True, True)]}
        for rows in ([], [valid, valid], [{"id": "empty", "checks": []}],
                     [{"id": "duplicate", "checks": valid["checks"] * 2}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize(rows)

    def test_failure_is_not_hidden_by_other_successes(self):
        report = summarize([{"id": "fixture", "checks": [
            Check("a", "safety", False, False), Check("b", "safety", True, False),
            Check("c", "quality", "good", "good")]}])
        self.assertFalse(report["passed"])
        self.assertEqual(report["metrics"]["safety"], {"passed": 1, "total": 2, "rate": 0.5})
        self.assertEqual(report["scenarios"], {"passed": 0, "total": 1})
        self.assertFalse(Check("strict", "safety", 1, True).passed)

    def test_unexpected_exception_stays_in_failure_denominator_without_details(self):
        def broken():
            raise RuntimeError("private fixture detail")
        report = summarize([guarded_row("broken", broken)])
        self.assertFalse(report["passed"])
        self.assertEqual(report["scenarios"]["total"], 1)
        self.assertNotIn("private fixture detail", json.dumps(report))

    def test_grader_detects_wrong_expectations(self):
        with offline(), TemporaryDirectory() as directory:
            f = Fixture(directory)
            wrong = replace(AGENT_CASES[0], error="TIMEOUT", calls=0, tools=(), pending=1)
            with f.scope():
                row = agent_row(wrong, "loop", f)
        failures = {c.name for c in row["checks"] if not c.passed}
        self.assertTrue({"status", "failure_code", "provider_calls", "successful_tool_sequence", "proposal_count"} <= failures)
        self.assertFalse(summarize([row])["passed"])

    def test_grader_detects_premature_final_instead_of_required_tool(self):
        with offline(), TemporaryDirectory() as directory, patch.object(ScriptedProvider, "generate",
                return_value=LLMResponse(text=json.dumps(FINAL), finish_reason="stop")):
            f = Fixture(directory)
            with f.scope():
                row = agent_row(AGENT_CASES[0], "graph", f)
        self.assertFalse(next(c for c in row["checks"] if c.name == "successful_tool_sequence").passed)
        self.assertFalse(summarize([row])["passed"])

    def test_response_grader_rejects_unsupported_execution_claim(self):
        forged = AgentResult(status="INFORMATIONAL", customer_response="Refund executed", steps_used=1, history=())
        self.assertFalse(response_checks(forged, [], False)[0].passed)

    def test_retrieval_metric_math_includes_missing_and_irrelevant_evidence(self):
        metrics = retrieval_metrics(["wrong", "right", "right"], {"right", "missing"})
        self.assertAlmostEqual(metrics["precision_at_k"], 2 / 3)
        self.assertEqual(metrics["document_recall_at_k"], 0.5)
        self.assertEqual(metrics["reciprocal_rank"], 0.5)
        self.assertEqual(retrieval_metrics([], {"right"}), {
            "precision_at_k": None, "document_recall_at_k": 0.0, "reciprocal_rank": 0.0})
        self.assertEqual(retrieval_metrics([], set()), {
            "precision_at_k": None, "document_recall_at_k": None, "reciprocal_rank": None})

    def test_offline_guard_records_attempts_even_when_caught(self):
        with offline() as attempts:
            with self.assertRaises(RuntimeError):
                socket.create_connection(("invalid.example", 443))
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo("invalid.example", 443)
            with socket.socket() as connection, self.assertRaises(RuntimeError):
                connection.connect(("127.0.0.1", 1))
        self.assertEqual(len(attempts), 3)

    def test_scripts_cannot_silently_supply_unplanned_steps(self):
        provider = ScriptedProvider([])
        with self.assertRaises(RuntimeError):
            provider.generate(None, response_schema={})
        self.assertTrue(provider.exhausted)
        self.assertEqual(len(provider.requests), 1)

    def test_cli_prints_serializable_report_and_sets_exit_status(self):
        for passed, expected in ((True, 0), (False, 1)):
            report = {**self.report, "passed": passed}
            stream = io.StringIO()
            with patch("evaluations.phase15.evaluate", return_value=report), redirect_stdout(stream):
                self.assertEqual(main(), expected)
            self.assertEqual(json.loads(stream.getvalue()), report)

    def test_report_contains_no_credentials_or_runtime_identifiers(self):
        text = json.dumps(self.report, allow_nan=False)
        for token in ("phase15-customer-fixture", "phase15-reviewer-fixture", "conversation_id", "workflow_id"):
            self.assertNotIn(token, text)
        self.assertEqual(self.report["metrics"]["harness_integrity"]["rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
