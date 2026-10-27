"""Phase 9 graph routing and parity tests; all generation is scripted and local."""

from datetime import datetime, timedelta, timezone
import json
import os
from unittest.mock import Mock, patch
from uuid import uuid4

from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

import test_agent as baseline
from backend.app.agent import AgentConfig, ResolutionAgent
from backend.app.bedrock import BedrockProvider
from backend.app.graph_agent import GraphResolutionAgent, GraphState
from backend.app.llm import StructuredLLM
from backend.app.operations import AuthoritativeDeliveryFact
from backend.app.schemas import CaseCreate


class GraphAgentTests(baseline.AgentTests):
    # Reuse all 15 Phase 8 scenarios against the graph, not the old loop.
    def run_script(self, script, max_steps=8, request=None):
        self.provider = baseline.ScriptedProvider(script)
        self.agent = GraphResolutionAgent(StructuredLLM(self.provider), self.tools, AgentConfig(max_steps=max_steps))
        self.state = self.agent.run_state(request or self.request)
        return self.state.final_result

    def test_case_mismatch_prevents_proposal_creation(self):
        # The graph, unlike the legacy loop, binds proposal identity to the
        # immutable request before validating or dispatching the tool call.
        result = self.run_script([
            self.proposal(case_id=str(uuid4())), baseline.FINAL,
        ])
        self.assertEqual(result.status, "HUMAN_REVIEW_REQUIRED")
        self.assertIsNone(result.error)
        proposals = self.proposals.list_for_case(self.case.id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].case_id, self.case.id)
        self.assertEqual(self.state.history[-2].decision.arguments["case_id"], str(self.case.id))

    def test_explicit_routing_and_loop_nodes(self):
        self.run_script([baseline.call("get_inventory", sku="LAP-1"), baseline.FINAL])
        self.assertEqual(self.state.node_path, (
            "load_case", "reason", "execute_tool", "record_result", "reason", "finalize",
        ))
        self.assertEqual(GraphState.model_validate_json(self.state.model_dump_json()), self.state)
        self.assertIsNone(self.state.staged_event)
        self.assertIsNone(self.agent._graph.checkpointer)
        self.assertIsNone(self.agent._graph.store)

    def test_nova_lite_format_tool_keeps_graph_tool_and_final_decisions_local(self):
        def formatted(value):
            return {"stopReason": "tool_use", "output": {"message": {
                "role": "assistant", "content": [{"toolUse": {
                    "toolUseId": "tooluse_offline",
                    "name": ("submit_final_decision" if value["decision"]["kind"] == "final"
                             else "submit_tool_decision"),
                    "input": value}}]}},
                "usage": {"inputTokens": 7, "outputTokens": 8, "totalTokens": 15}}

        client = Mock()
        client.converse.side_effect = [
            formatted(baseline.call("get_inventory", sku="LAP-1")),
            formatted(baseline.FINAL),
        ]
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=client)
        agent = GraphResolutionAgent(StructuredLLM(provider), self.tools)
        with patch.object(self.tools, "invoke", wraps=self.tools.invoke) as invoke:
            state = agent.run_state(self.request)
        self.assertEqual(state.final_result.status, "INFORMATIONAL")
        self.assertEqual(state.node_path, (
            "load_case", "reason", "execute_tool", "record_result", "reason", "finalize"))
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ["get_case", "get_inventory"])
        self.assertEqual(client.converse.call_count, 2)
        self.assertTrue(all("toolConfig" in call.kwargs for call in client.converse.call_args_list))
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 0)

    def test_nova_lite_format_tool_preserves_pending_human_review(self):
        client = Mock()
        client.converse.side_effect = [{"stopReason": "tool_use", "output": {"message": {
            "role": "assistant", "content": [{"toolUse": {
                "toolUseId": "tooluse_offline",
                "name": ("submit_final_decision" if value["decision"]["kind"] == "final"
                         else "submit_tool_decision"),
                "input": value}}]}},
            "usage": {"inputTokens": 7, "outputTokens": 8, "totalTokens": 15}}
            for value in (self.proposal(), baseline.FINAL)]
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=client)
        result = GraphResolutionAgent(StructuredLLM(provider), self.tools).run(self.request)
        self.assertEqual(result.status, "HUMAN_REVIEW_REQUIRED")
        self.assertEqual(len(result.pending_proposal_ids), 1)
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
        self.assertEqual(self.proposals.list_for_case(self.case.id)[0].status, "PENDING_REVIEW")
        self.assertFalse(result.actions_executed)
        self.assertEqual(client.converse.call_count, 2)

    def test_failure_edges_keep_audit_once(self):
        self.run_script([baseline.call("approve")])
        self.assertEqual(self.state.node_path, ("load_case", "reason", "execute_tool", "record_result", "fail_safely"))
        self.assertEqual(len(self.state.history), 2)
        self.assertEqual(self.state.history[-1].error, "UNKNOWN_TOOL")
        self.run_script(["malformed"])
        self.assertEqual(self.state.node_path, ("load_case", "reason", "fail_safely"))
        self.assertEqual(len(self.state.history), 2)

    def test_bedrock_output_schema_failure_uses_grounded_deterministic_fallback(self):
        # This exercises the post-validation path without contacting Bedrock.
        # The default graph remains fail-closed; only the Bedrock HITL wiring
        # opts into this safe fallback.
        self.provider = baseline.ScriptedProvider([{"decision": {"kind": "complete"}}])
        self.agent = GraphResolutionAgent(
            StructuredLLM(self.provider), self.tools,
            deterministic_fallback_on_invalid_response=True,
        )
        state = self.agent.run_state(self.request)

        self.assertEqual(state.final_result.status, "INFORMATIONAL")
        self.assertIsNone(state.final_result.error)
        failure = next(event.llm_failure for event in state.final_result.history
                       if event.llm_failure is not None)
        self.assertEqual((failure.code, failure.invalid_response_stage),
                         ("INVALID_RESPONSE", "output_schema"))
        self.assertEqual(state.final_result.resolution_draft.response_source,
                         "DETERMINISTIC_FALLBACK")
        self.assertEqual(state.final_result.history[-1].error,
                         "EARLY_DETERMINISTIC_FALLBACK")
        self.assertFalse(state.final_result.actions_executed)

    def test_final_on_last_step_succeeds_without_extra_reasoning(self):
        result = self.run_script([baseline.call("get_inventory", sku="LAP-1"), baseline.FINAL], max_steps=2)
        self.assertEqual(result.status, "INFORMATIONAL")
        self.assertEqual(len(self.provider.requests), 2)

    def test_repeated_knowledge_search_finishes_without_second_tool_execution(self):
        case = self.cases.create_case(CaseCreate(
            customer_id=self.case.customer_id, order_id=self.order.id,
            subject="Laptop not working", description="The laptop stopped working.",
        ))
        request = baseline.AgentRequest(
            case_id=case.id, message="Please investigate the technical issue.",
        )
        self.provider = baseline.ScriptedProvider([
            baseline.call("search_knowledge", query="laptop troubleshooting", top_k=3),
            baseline.call("search_knowledge", query="laptop not working", top_k=3),
        ])
        self.agent = GraphResolutionAgent(
            StructuredLLM(self.provider), self.tools, AgentConfig(max_steps=8),
        )
        with patch.object(self.tools, "invoke", wraps=self.tools.invoke) as invoke:
            state = self.agent.run_state(request)

        self.assertEqual(state.final_result.status, "INFORMATIONAL")
        self.assertIsNone(state.final_result.error)
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ["get_case", "search_knowledge"])
        repeated = next(event for event in state.final_result.history if event.error == "REPEATED_TOOL_CALL")
        self.assertEqual(repeated.decision.name, "search_knowledge")
        self.assertEqual(state.final_result.history[-1].error, "EARLY_DETERMINISTIC_FALLBACK")
        self.assertEqual(state.final_result.history[-1].decision.kind, "final")
        self.assertEqual(state.final_result.resolution_draft.response_source, "DETERMINISTIC_FALLBACK")
        self.assertFalse(state.final_result.actions_executed)

    def test_long_run_uses_application_limit_not_default_graph_limit(self):
        result = self.run_script([baseline.call("get_inventory", sku="LAP-1")] * 32, max_steps=32)
        self.assertEqual(result.error, "STEP_LIMIT")
        self.assertEqual(len(self.provider.requests), 32)
        self.assertEqual(len(self.state.history), 33)
        self.assertEqual(self.state.node_path[-1], "fail_safely")

    def test_behavioral_parity_for_read_results_and_failures(self):
        scripts = [
            [baseline.FINAL],
            [baseline.call("get_inventory", sku="LAP-1"), baseline.FINAL],
            [baseline.call("approve")],
            ["bad JSON"],
            [baseline.call("get_order", order_id="bad")],
            [baseline.call("get_inventory", sku="LAP-1")] * 2,
        ]
        for script in scripts:
            with self.subTest(script=script):
                original_provider = baseline.ScriptedProvider(script)
                original = ResolutionAgent(StructuredLLM(original_provider), self.tools, AgentConfig(max_steps=2))
                expected = original.run(self.request)
                actual = self.run_script(script, max_steps=2)
                self.assertEqual(actual, expected)
                # LangGraph adds server-selected, citable policy context; the
                # legacy loop deliberately remains unchanged.
                graph_context = json.loads(self.provider.requests[0].messages[-1].content)
                self.assertIn("grounded_policy_evidence", graph_context)
                self.assertTrue(graph_context["grounded_policy_evidence"]["policy_passages"])

    def test_proposal_behavioral_parity_ignoring_server_ids_and_timestamps(self):
        now = datetime.now(timezone.utc)
        self.delivery_ingestion.ingest(self.delivery_source.id, AuthoritativeDeliveryFact(
            id=uuid4(), case_id=self.case.id, order_id=self.order.id,
            customer_id=self.case.customer_id, sku="LAP-1", quantity=1,
            delivered_at=now - timedelta(days=10), reason="wrong_item", condition="unused",
            source_id=self.delivery_source.id,
            source_reference="test-verified-delivery", verified_at=now,
        ))

        refund_request = baseline.AgentRequest(
            case_id=self.case.id, message="Request a refund for the selected item.",
            resolution_intent="refund", refund_sku="LAP-1",
        )
        actual = self.run_script([baseline.FINAL], request=refund_request)

        self.assertEqual(actual.status, "HUMAN_REVIEW_REQUIRED")
        self.assertEqual(len(actual.pending_proposal_ids), 1)
        proposal = self.proposals.get(actual.pending_proposal_ids[0])
        self.assertEqual(proposal.status, "PENDING_REVIEW")
        self.assertEqual(proposal.case_id, self.case.id)
        self.assertEqual(proposal.action, "refund")
        self.assertFalse(actual.actions_executed)
        self.assertEqual(self.provider.requests, [])

    def test_invalid_state_and_public_request_cannot_inject_run_history(self):
        with self.assertRaises(ValidationError):
            GraphState(request=self.request, steps=-1)
        with self.assertRaises(ValidationError):
            GraphState(request=self.request, steps=True)
        with self.assertRaises(ValidationError):
            GraphState(request=self.request, arbitrary_function="approve")
        agent = GraphResolutionAgent(StructuredLLM(baseline.ScriptedProvider([])), self.tools)
        with self.assertRaises(ValidationError):
            agent.run({**self.request.model_dump(), "created": False, "history": []})

    def test_engine_failure_retains_last_completed_snapshot(self):
        for exception, code in ((GraphRecursionError("private"), "GRAPH_LIMIT"), (RuntimeError("private"), "GRAPH_ERROR")):
            with self.subTest(code=code):
                self.run_script([baseline.call("get_inventory", sku="LAP-1"), baseline.FINAL])
                snapshot = self.state.model_dump()
                snapshot["final_result"] = None
                def broken_stream(*args, **kwargs):
                    yield snapshot
                    raise exception
                with patch.object(self.agent._graph, "stream", side_effect=broken_stream):
                    result = self.agent.run(self.request)
                self.assertEqual(result.error, code)
                self.assertEqual(result.history, self.state.history)
                self.assertNotIn("private", result.model_dump_json())

    def test_environment_tracing_cannot_send_audit_to_langsmith(self):
        # The package is transitive only; even an inherited tracing setting must not cause I/O.
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "test-placeholder"}), \
                patch("langsmith.client.Client.create_run", side_effect=AssertionError("External tracing")) as create_run:
            result = self.run_script([baseline.FINAL])
            self.assertEqual(result.status, "INFORMATIONAL")
            create_run.assert_not_called()

    def test_engine_failure_after_write_preserves_staged_pending_proposal(self):
        self.run_script([self.proposal(), baseline.FINAL])
        snapshot = self.state.model_dump()
        event = self.state.history[1]
        snapshot.update(final_result=None, pending={}, created=False,
                        history=self.state.history[:1], staged_event=event, decision=event.decision, steps=1)
        def broken_stream(*args, **kwargs):
            yield snapshot
            raise GraphRecursionError("interrupted after execution")
        with patch.object(self.agent._graph, "stream", side_effect=broken_stream):
            result = self.agent.run(self.request)
        self.assertEqual(result.error, "GRAPH_LIMIT")
        self.assertEqual(result.pending_proposal_ids, (event.result.data.id,))
        self.assertEqual(len(result.history), 2)
        self.assertEqual(len(self.proposals.list_for_case(self.case.id)), 1)
