"""Offline Bedrock Converse adapter checks; the client is always mocked."""

import json
import sys
import types
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from botocore.exceptions import ClientError
from pydantic import ValidationError

from backend.app.bedrock import BedrockProvider
from backend.app.llm import LLMMessage, LLMRequest, ProviderFailure, StructuredLLM
from backend.app.agent import AgentDecision
from backend.app.observability import LocalObserver, observing
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated


FINAL = '{"decision":{"kind":"final"}}'


def completion(text=FINAL, reason="end_turn", usage=None):
    result = {"stopReason": reason, "output": {"message": {
        "role": "assistant", "content": [{"text": text}]}}}
    if usage is not None:
        result["usage"] = usage
    return result


def tool_completion(value, *, name="submit_final_decision", reason="tool_use", blocks=None,
                    wrapped=False):
    content = blocks if blocks is not None else [{"toolUse": {
        "toolUseId": "tooluse_offline", "name": name,
        "input": {"response": value} if wrapped else value}}]
    return {"stopReason": reason, "output": {"message": {
        "role": "assistant", "content": content}},
        "usage": {"inputTokens": 7, "outputTokens": 8, "totalTokens": 15}}


class BedrockTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.converse.return_value = completion()
        self.provider = BedrockProvider(model_id="test.model-v1", client=self.client)
        self.request = LLMRequest(messages=(
            LLMMessage(role="system", content="Business instructions"),
            LLMMessage(role="user", content="Question"),
            LLMMessage(role="assistant", content=FINAL),
            LLMMessage(role="user", content="Follow-up")), max_output_tokens=2048)

    def result(self):
        return StructuredLLM(self.provider).generate(self.request, AgentDecision)

    def test_converse_messages_schema_budget_and_no_native_tools(self):
        schema = AgentDecision.model_json_schema()
        before = json.dumps(schema, sort_keys=True)
        response = self.provider.generate(self.request, response_schema=schema)
        self.assertEqual(response.text, FINAL)
        self.client.converse.assert_called_once()
        body = self.client.converse.call_args.kwargs
        self.assertEqual(body["modelId"], "test.model-v1")
        self.assertEqual(body["inferenceConfig"], {"maxTokens": 2048, "temperature": 0})
        self.assertEqual(body["messages"], [
            {"role": "user", "content": [{"text": "Question"}]},
            {"role": "assistant", "content": [{"text": FINAL}]},
            {"role": "user", "content": [{"text": "Follow-up"}]}])
        self.assertEqual(body["system"][0], {"text": "Business instructions"})
        instruction = body["system"][-1]["text"]
        for phrase in ("first non-whitespace character must be {", "last must be }",
                       "double-quoted JSON keys and strings", "trailing commas",
                       "Markdown code fences", "text after the object"):
            self.assertIn(phrase, instruction)
        self.assertEqual(json.loads(body["system"][-1]["text"].split("JSON Schema: ", 1)[1]), schema)
        self.assertEqual(json.dumps(schema, sort_keys=True), before)
        self.assertNotIn("toolConfig", body)

    def test_stop_reasons_usage_and_reasoning(self):
        self.client.converse.return_value = completion(usage={
            "inputTokens": 4, "outputTokens": 5, "totalTokens": 9})
        self.assertTrue(self.result().ok)
        self.assertEqual(self.provider.generate(self.request, response_schema={}).usage.model_dump(),
                         {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9})
        self.client.converse.return_value = completion(usage={
            "inputTokens": True, "outputTokens": -1, "totalTokens": "unknown"})
        self.assertEqual(self.provider.generate(self.request, response_schema={}).usage.model_dump(),
                         {"input_tokens": None, "output_tokens": None, "total_tokens": None})
        for reason, finish in (("end_turn", "stop"), ("stop_sequence", "stop"),
                               ("max_tokens", "length"),
                               ("model_context_window_exceeded", "length"),
                               ("content_filtered", "refusal"),
                               ("guardrail_intervened", "refusal")):
            with self.subTest(reason=reason):
                self.client.converse.return_value = completion(reason=reason)
                self.assertEqual(self.provider.generate(self.request, response_schema={}).finish_reason,
                                 finish)
        self.client.converse.return_value = completion()
        self.client.converse.return_value["output"]["message"]["content"].insert(
            0, {"reasoningContent": {"reasoningText": {"text": "PRIVATE"}}})
        self.assertEqual(self.provider.generate(self.request, response_schema={}).text, FINAL)

    def test_unknown_stop_tool_use_and_malformed_output_fail_closed(self):
        for payload in ({}, [], completion(reason="tool_use"),
                        {"stopReason": "end_turn", "output": {"message": {
                            "role": "assistant", "content": [{"toolUse": {"name": "approve"}}]}}},
                        completion(reason="malformed_model_output")):
            with self.subTest(payload=payload):
                self.client.converse.return_value = payload
                self.assertEqual(self.result().code, "INVALID_RESPONSE")

    def test_nova_lite_uses_distinct_required_decision_tools_with_the_existing_schema(self):
        for model_id in ("amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0",
                         "eu.amazon.nova-lite-v1:0"):
            with self.subTest(model_id=model_id):
                self.client.converse.reset_mock()
                self.client.converse.return_value = tool_completion(json.loads(FINAL))
                provider = BedrockProvider(model_id=model_id, client=self.client)
                result = StructuredLLM(provider).generate(self.request, AgentDecision)
                self.assertTrue(result.ok)
                self.assertEqual(result.data.decision.kind, "final")
                self.client.converse.assert_called_once()
                body = self.client.converse.call_args.kwargs
                self.assertEqual(body["modelId"], model_id)
                self.assertEqual(body["inferenceConfig"], {"maxTokens": 2048, "temperature": 0})
                if model_id == "us.amazon.nova-lite-v1:0":
                    self.assertEqual(body["additionalModelRequestFields"],
                                     {"inferenceConfig": {"topK": 1}})
                else:
                    self.assertNotIn("additionalModelRequestFields", body)
                config = body["toolConfig"]
                self.assertEqual(config["toolChoice"], {"any": {}})
                specs = {item["toolSpec"]["name"]: item["toolSpec"] for item in config["tools"]}
                self.assertEqual(set(specs), {"submit_final_decision", "submit_tool_decision"})
                for spec in specs.values():
                    tool_schema = spec["inputSchema"]["json"]
                    self.assertEqual(set(tool_schema), {"type", "properties", "required"})
                    self.assertEqual(tool_schema["type"], "object")
                    self.assertEqual(tool_schema["required"], ["decision"])
                final = specs["submit_final_decision"]["inputSchema"]["json"]["properties"]["decision"]
                self.assertEqual(final, {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["final"]}}, "required": ["kind"]})
                self.assertEqual(
                    specs["submit_final_decision"]["inputSchema"]["json"]["properties"]["grounded_response"],
                    {"type": "object"},
                )
                tool = specs["submit_tool_decision"]["inputSchema"]["json"]["properties"]["decision"]
                self.assertEqual(tool, {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["tool"]}, "name": {"type": "string"},
                    "arguments": {"type": "object"}}, "required": ["kind", "name", "arguments"]})
                self.assertEqual(json.loads(body["system"][-1]["text"].split("JSON Schema: ", 1)[1]),
                                 AgentDecision.model_json_schema())
                self.assertIn("formatting tools do not execute application actions",
                              body["system"][-1]["text"])
                self.assertNotIn("outputConfig", body)

    def test_nova_lite_final_tool_accepts_grounded_response_for_local_validation(self):
        value = {"decision": {"kind": "final"}, "grounded_response": {
            "issue_type": "malfunction", "recommended_decision": "REQUEST_EVIDENCE",
            "verified_facts": ["case_id:case-1"], "missing_evidence": ["diagnostic evidence"],
            "policy_passages": [{
                "document_id": "warranty-policy", "chunk_id": "a" * 64,
                "title": "Warranty guidance", "version": "v1",
                "source_uri": "local-knowledge://warranty-policy", "passage": "Use safe diagnostics.",
            }],
            "decision_reason": "Verified records require diagnostic evidence before support review.",
            "customer_facing_response": (
                "Please share the requested diagnostic evidence. No refund, replacement, return, or payment "
                "action has been executed."
            ),
        }}
        self.client.converse.return_value = tool_completion(value)
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)

        result = StructuredLLM(provider).generate(self.request, AgentDecision)

        self.assertTrue(result.ok)
        self.assertEqual(result.data.grounded_response.customer_facing_response,
                         value["grounded_response"]["customer_facing_response"])

    def test_nova_lite_rejects_text_fallback_and_ambiguous_tool_envelopes(self):
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        correct = tool_completion(json.loads(FINAL))["output"]["message"]["content"][0]
        cases = (completion(FINAL), tool_completion(json.loads(FINAL), name="get_inventory"),
                 tool_completion(json.loads(FINAL), name="submit_json_response"),
                 tool_completion(json.loads(FINAL), name=["submit_final_decision"]),
                 tool_completion(json.loads(FINAL), reason="end_turn"),
                 tool_completion(json.loads(FINAL), blocks=[]),
                 tool_completion(json.loads(FINAL), blocks=[correct, correct]),
                 tool_completion(json.loads(FINAL), blocks=[{"text": FINAL}]),
                 tool_completion(json.loads(FINAL), blocks=[{"toolUse": {
                     "toolUseId": "tooluse_offline", "name": "submit_json_response",
                     "input": {"response": json.loads(FINAL), "extra": "PRIVATE"}}}]),
                 tool_completion(json.loads(FINAL), blocks=[{"toolUse": {
                     "toolUseId": "tooluse_offline", "name": "submit_json_response",
                     "input": FINAL}}]),
                 tool_completion(json.loads(FINAL), blocks=[correct, {"toolUse": {
                     "toolUseId": "tooluse_other", "name": "approve", "input": {}}}]),
                 tool_completion(json.loads(FINAL), blocks=[correct, {"unknown": "PRIVATE"}]))
        for payload in cases:
            with self.subTest(payload_type=payload["stopReason"], content=payload["output"]["message"]["content"]):
                self.client.converse.return_value = payload
                result = StructuredLLM(provider).generate(self.request, AgentDecision)
                self.assertEqual(result.code, "INVALID_RESPONSE")
                self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_nova_lite_tool_input_still_passes_strict_json_and_schema_validation(self):
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        for value in ({"decision": {"kind": "final", "extra": "PRIVATE"}},
                      {"decision": {"kind": "tool", "name": "get_inventory"}},
                      {"decision": {"kind": "tool", "name": "approve", "arguments": []}},
                      {"decision": {"kind": "tool", "name": "get_case", "arguments": {},
                                    "extra": "PRIVATE"}}):
            with self.subTest(value=value):
                name = "submit_final_decision" if value["decision"]["kind"] == "final" else "submit_tool_decision"
                self.client.converse.return_value = tool_completion(value, name=name)
                self.assertEqual(StructuredLLM(provider).generate(self.request, AgentDecision).code,
                                 "INVALID_RESPONSE")
        self.client.converse.return_value = tool_completion({"decision": {"kind": "final"},
                                                              "non_finite": float("nan")})
        self.assertEqual(StructuredLLM(provider).generate(self.request, AgentDecision).code,
                         "INVALID_RESPONSE")
        self.client.converse.return_value = tool_completion(json.loads(FINAL), blocks=[
            {"text": "PRIVATE model preamble"},
            {"toolUse": {"toolUseId": "tooluse_offline", "name": "submit_final_decision",
                         "input": json.loads(FINAL)}}])
        self.assertTrue(StructuredLLM(provider).generate(self.request, AgentDecision).ok)

    def test_nova_lite_reproduces_final_extra_fields_without_discarding_them(self):
        value = {"decision": {"kind": "final", "name": "get_case", "arguments": {}}}
        with self.assertRaises(ValidationError) as caught:
            AgentDecision.model_validate(value, strict=True)
        self.assertEqual([(issue["loc"], issue["type"]) for issue in caught.exception.errors()], [
            (("decision", "final", "name"), "extra_forbidden"),
            (("decision", "final", "arguments"), "extra_forbidden")])
        self.client.converse.return_value = tool_completion(value)
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        self.assertEqual(json.loads(provider.generate(self.request,
            response_schema=AgentDecision.model_json_schema()).text), value)
        self.assertEqual(StructuredLLM(provider).generate(self.request, AgentDecision).code,
                         "INVALID_RESPONSE")

    def test_nova_lite_accepts_both_valid_decisions_and_rejects_crossed_tool_names(self):
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        values = ((json.loads(FINAL), "submit_final_decision"),
                  ({"decision": {"kind": "tool", "name": "get_case",
                                  "arguments": {"case_id": "case-1"}}}, "submit_tool_decision"))
        for value, name in values:
            with self.subTest(name=name):
                self.client.converse.return_value = tool_completion(value, name=name)
                result = StructuredLLM(provider).generate(self.request, AgentDecision)
                self.assertTrue(result.ok)
                self.assertEqual(result.data.model_dump(mode="json"), {
                    **value, "investigation_plan": None, "resolution_draft": None,
                    "grounded_response": None,
                })
                other = "submit_tool_decision" if name == "submit_final_decision" else "submit_final_decision"
                self.client.converse.return_value = tool_completion(value, name=other)
                self.assertEqual(StructuredLLM(provider).generate(self.request, AgentDecision).code,
                                 "INVALID_RESPONSE")

    def test_nova_lite_schema_projection_rejects_stringified_arguments_without_coercion(self):
        schema = AgentDecision.model_json_schema()
        self.assertEqual(schema["$defs"]["JsonValue"], {})
        self.assertEqual(schema["$defs"]["ToolCall"]["properties"]["arguments"]["type"], "object")
        self.client.converse.return_value = tool_completion({"decision": {"kind": "tool",
            "name": "get_case", "arguments": '{"case_id":"PRIVATE"}'}}, name="submit_tool_decision")
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        result = StructuredLLM(provider).generate(self.request, AgentDecision)
        self.assertEqual(result.code, "INVALID_RESPONSE")
        self.assertNotIn("PRIVATE", result.model_dump_json())
        tool_schema = self.client.converse.call_args.kwargs["toolConfig"]["tools"][1]["toolSpec"]["inputSchema"]["json"]
        self.assertEqual(tool_schema["properties"]["decision"]["properties"]["arguments"], {"type": "object"})

    def test_nova_pro_uses_two_forced_tools_for_valid_final_and_tool_decisions(self):
        values = ((json.loads(FINAL), "submit_final_decision"),
                  ({"decision": {"kind": "tool", "name": "get_case",
                                  "arguments": {"case_id": "case-1"}}}, "submit_tool_decision"))
        for model_id in ("amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"):
            provider = BedrockProvider(model_id=model_id, client=self.client)
            for value, name in values:
                with self.subTest(model_id=model_id, name=name):
                    self.client.converse.reset_mock()
                    self.client.converse.return_value = tool_completion(value, name=name)
                    result = StructuredLLM(provider).generate(self.request, AgentDecision)
                    self.assertTrue(result.ok)
                    self.assertEqual(result.data.model_dump(mode="json"), {
                        **value, "investigation_plan": None, "resolution_draft": None,
                        "grounded_response": None,
                    })
                    self.client.converse.assert_called_once()
                    body = self.client.converse.call_args.kwargs
                    self.assertEqual(body["modelId"], model_id)
                    self.assertEqual(body["toolConfig"]["toolChoice"], {"any": {}})
                    specs = {item["toolSpec"]["name"]: item["toolSpec"]["inputSchema"]["json"]
                             for item in body["toolConfig"]["tools"]}
                    self.assertEqual(set(specs), {"submit_final_decision", "submit_tool_decision"})
                    self.assertEqual(set(specs[name]), {"type", "properties", "required"})
                    self.assertEqual(specs[name]["required"], ["decision"])
                    self.assertNotIn("outputConfig", body)

    def test_nova_pro_rejects_text_malformed_multiple_and_crossed_tool_responses(self):
        final = json.loads(FINAL)
        correct = tool_completion(final)["output"]["message"]["content"][0]
        cases = (completion(FINAL), tool_completion(final, reason="end_turn"),
                 tool_completion(final, blocks=[]),
                 tool_completion(final, blocks=[correct, correct]),
                 tool_completion(final, name="approve"),
                 tool_completion(final, blocks=[{"toolUse": {
                     "toolUseId": "offline", "name": "submit_final_decision", "input": FINAL}}]),
                 tool_completion(final, name="submit_tool_decision"),
                 tool_completion({"decision": {"kind": "tool", "name": "get_case",
                     "arguments": {}}}, name="submit_final_decision"))
        for model_id in ("amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"):
            provider = BedrockProvider(model_id=model_id, client=self.client)
            for index, payload in enumerate(cases):
                with self.subTest(model_id=model_id, case=index):
                    self.client.converse.return_value = payload
                    result = StructuredLLM(provider).generate(self.request, AgentDecision)
                    self.assertEqual(result.code, "INVALID_RESPONSE")

    def test_nova_pro_preserves_extra_fields_and_rejects_stringified_arguments(self):
        cases = (({"decision": {"kind": "final", "name": "get_case", "arguments": {}}},
                  "submit_final_decision"),
                 ({"decision": {"kind": "tool", "name": "get_case", "arguments": {},
                                "extra": "PRIVATE"}}, "submit_tool_decision"),
                 ({"decision": {"kind": "tool", "name": "get_case",
                                "arguments": '{"case_id":"PRIVATE"}'}}, "submit_tool_decision"))
        for model_id in ("amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"):
            provider = BedrockProvider(model_id=model_id, client=self.client)
            for value, name in cases:
                with self.subTest(model_id=model_id, name=name, value=value):
                    self.client.converse.return_value = tool_completion(value, name=name)
                    raw = provider.generate(self.request,
                                            response_schema=AgentDecision.model_json_schema())
                    self.assertEqual(json.loads(raw.text), value)
                    result = StructuredLLM(provider).generate(self.request, AgentDecision)
                    self.assertEqual(result.code, "INVALID_RESPONSE")
                    self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_nova_lite_other_response_models_keep_wrapped_tool_contract(self):
        from pydantic import BaseModel

        class Summary(BaseModel):
            summary: str

        self.client.converse.return_value = tool_completion({"summary": "done"},
            name="submit_json_response", wrapped=True)
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        result = StructuredLLM(provider).generate(self.request, Summary)
        self.assertTrue(result.ok)
        schema = self.client.converse.call_args.kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        self.assertEqual(schema["required"], ["response"])

    def test_nova_lite_agent_schema_drift_fails_before_client_call(self):
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=self.client)
        schema = AgentDecision.model_json_schema()
        schema["$defs"]["ToolCall"]["properties"]["arguments"]["type"] = "string"
        with self.assertRaisesRegex(ValueError, "Unsupported AgentDecision schema"):
            provider.generate(self.request, response_schema=schema)
        self.client.converse.assert_not_called()

    def test_safe_errors_without_retry(self):
        class ServiceError(Exception):
            def __init__(self, code):
                super().__init__("secret provider detail")
                self.response = {"Error": {"Code": code}}

        for error, code in ((TimeoutError("secret"), "TIMEOUT"),
                            (ServiceError("ModelTimeoutException"), "TIMEOUT"),
                            (ServiceError("AccessDeniedException"), "UNAVAILABLE")):
            with self.subTest(code=code):
                self.client.converse.reset_mock(side_effect=True)
                self.client.converse.side_effect = error
                with self.assertRaises(ProviderFailure) as caught:
                    self.provider.generate(self.request, response_schema={})
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))
                self.client.converse.assert_called_once()
        self.client.converse.side_effect = RuntimeError("adapter bug")
        with self.assertRaisesRegex(RuntimeError, "adapter bug"):
            self.provider.generate(self.request, response_schema={})

    def test_bedrock_http_status_is_safe_telemetry_and_public_failure_stays_unavailable(self):
        auth = LocalAuthenticationProvider((LocalCredential(token="offline-status-test-token",
            identity=AuthenticatedIdentity(user_id=UUID(int=17), role="REVIEWER")),))
        self.enterContext(authenticated(auth, "offline-status-test-token"))
        request = LLMRequest(messages=(LLMMessage(role="user", content="PRIVATE_PROMPT"),))
        for service_code, status in (("ValidationException", 400), ("ModelErrorException", 424),
                                     ("ThrottlingException", 429), ("ServiceUnavailableException", 503)):
            with self.subTest(service_code=service_code):
                self.client.converse.reset_mock(side_effect=True)
                self.client.converse.side_effect = ClientError({
                    "Error": {"Code": service_code, "Message": "PRIVATE_AWS_ERROR"},
                    "ResponseMetadata": {"HTTPStatusCode": status, "private": "PRIVATE_CREDENTIAL"}},
                    "Converse")
                observer = LocalObserver()
                with observing(observer):
                    result = StructuredLLM(BedrockProvider(
                        model_id="us.amazon.nova-lite-v1:0", client=self.client)).generate(
                            request, AgentDecision)
                self.assertEqual(result.model_dump(), {"ok": False, "code": "UNAVAILABLE",
                    "message": "Provider could not complete the request"})
                event = observer.snapshot()["events"][-1]
                self.assertEqual(event["provider_status_code"], status)
                self.assertEqual(event["provider"], "bedrock")
                self.assertTrue(event["provider_called"])
                self.assertIsNone(event["input_tokens"])
                for private in ("PRIVATE_PROMPT", "PRIVATE_AWS_ERROR", "PRIVATE_CREDENTIAL",
                                "offline-status-test-token"):
                    self.assertNotIn(private, json.dumps(observer.snapshot()))
                    self.assertNotIn(private, result.model_dump_json())
                self.client.converse.assert_called_once()

    def test_bedrock_ignores_non_numeric_or_out_of_range_http_status(self):
        auth = LocalAuthenticationProvider((LocalCredential(token="offline-status-test-token",
            identity=AuthenticatedIdentity(user_id=UUID(int=17), role="REVIEWER")),))
        self.enterContext(authenticated(auth, "offline-status-test-token"))
        for status in (True, "400", 99, 600, None):
            with self.subTest(status=status):
                self.client.converse.reset_mock(side_effect=True)
                self.client.converse.side_effect = ClientError({
                    "Error": {"Code": "ValidationException", "Message": "PRIVATE_AWS_ERROR"},
                    "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")
                observer = LocalObserver()
                with observing(observer):
                    result = StructuredLLM(BedrockProvider(
                        model_id="us.amazon.nova-lite-v1:0", client=self.client)).generate(
                            self.request, AgentDecision)
                self.assertEqual(result.code, "UNAVAILABLE")
                self.assertNotIn("provider_status_code", observer.snapshot()["events"][-1])

    def test_invalid_input_before_client_call(self):
        for kwargs in ({"model_id": " "}, {"model_id": "test", "timeout_ms": 0},
                       {"model_id": "test", "timeout_ms": True},
                       {"model_id": "test", "timeout_ms": 120001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BedrockProvider(client=self.client, **kwargs)
        with self.assertRaises(ValueError):
            self.provider.generate(LLMRequest(messages=(LLMMessage(role="system", content="only"),)),
                                   response_schema={})
        with self.assertRaises(ValueError):
            self.provider.generate(LLMRequest(messages=(
                LLMMessage(role="user", content="first"),
                LLMMessage(role="system", content="late"))), response_schema={})
        self.client.converse.assert_not_called()

    def test_owned_client_uses_bounded_timeouts_and_one_attempt(self):
        constructor = Mock()
        config_constructor = Mock(return_value=object())
        boto3 = types.ModuleType("boto3")
        boto3.client = constructor
        botocore = types.ModuleType("botocore")
        botocore.__path__ = []
        config = types.ModuleType("botocore.config")
        config.Config = config_constructor
        with patch.dict(sys.modules, {"boto3": boto3, "botocore": botocore,
                                      "botocore.config": config}):
            with BedrockProvider(model_id="test.model-v1", region_name="us-east-1",
                                 timeout_ms=1500):
                pass
        config_constructor.assert_called_once_with(
            connect_timeout=1.5, read_timeout=1.5,
            retries={"total_max_attempts": 1})
        constructor.assert_called_once_with("bedrock-runtime", region_name="us-east-1",
                                            config=config_constructor.return_value)
        constructor.return_value.close.assert_called_once()
        self.provider.close()
        self.client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
