"""Internal structured-response failure stages; no live providers or raw telemetry."""

import io
import json
import logging
import unittest
from unittest.mock import Mock, patch
from typing import get_args
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from backend.app.agent import AgentDecision
from backend.app.bedrock import BedrockProvider
from backend.app.frontend_contracts import DiagnosticsResponse, OutputSchemaError, TelemetryEvent
from backend.app.guardrails import MAX_DEPTH, MAX_PAYLOAD_CHARS, MAX_PROMPT_CHARS
from backend.app.llm import LLMFailure, LLMMessage, LLMRequest, LLMResponse, ProviderFailure, StructuredLLM, TokenUsage
from backend.app.main import create_app
from backend.app.observability import (
    LOGGER, MAX_SCHEMA_ERRORS, MAX_SCHEMA_LOCATION, LocalObserver, annotate, annotate_schema_validation, observing, span,
    SCHEMA_ERROR_TYPES, SCHEMA_LOCATION_LABELS,
)
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated


PROMPT = "PRIVATE-PROMPT-CUSTOMER-CONTEXT"
OUTPUT = "PRIVATE-MODEL-OUTPUT-CUSTOMER-DATA"
TOKEN = "PRIVATE-CREDENTIAL-TEST-ONLY"


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    count: int


class LLMDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.observer = LocalObserver()
        self.auth = LocalAuthenticationProvider((LocalCredential(token=TOKEN,
            identity=AuthenticatedIdentity(user_id=UUID(int=17), role="REVIEWER")),))
        self.enterContext(authenticated(self.auth, TOKEN))
        self.enterContext(observing(self.observer))
        self.request = LLMRequest(messages=(LLMMessage(role="user", content=PROMPT),))
        self.output = io.StringIO()
        handler = logging.StreamHandler(self.output)
        self.addCleanup(LOGGER.setLevel, LOGGER.level)
        self.addCleanup(LOGGER.removeHandler, handler)
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)

    def generate(self, raw, output_model=Summary):
        provider = Mock()
        provider.generate.return_value = raw
        result = StructuredLLM(provider).generate(self.request, output_model)
        provider.generate.assert_called_once()
        return result

    def response(self, text, reason="stop"):
        return LLMResponse(text=text, finish_reason=reason,
                           usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5))

    def assert_failure(self, result, stage):
        self.assertEqual(result.model_dump(), {"ok": False, "code": "INVALID_RESPONSE", "message":
            "Invalid provider response envelope" if stage == "provider_envelope"
            else "Output does not match the requested JSON model",
            "invalid_response_stage": stage})
        snapshot = self.observer.snapshot()
        event = snapshot["events"][-1]
        self.assertEqual(event["operation"], "llm")
        self.assertEqual(event["error"], "INVALID_RESPONSE")
        self.assertEqual(event["invalid_response_stage"], stage)
        self.assertTrue(event["provider_called"])
        self.assertEqual(event["total_tokens"], None if stage == "provider_envelope" else 5)
        logged = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(logged, event)
        for marker in (PROMPT, OUTPUT, TOKEN):
            self.assertNotIn(marker, json.dumps(snapshot))
            self.assertNotIn(marker, self.output.getvalue())
            self.assertNotIn(marker, result.model_dump_json())

    def test_provider_envelope_rejection_is_distinct_and_revalidates_instances(self):
        for raw in (None, {"text": OUTPUT, "finish_reason": TOKEN},
                    {"text": OUTPUT, "finish_reason": "stop", "credentials": TOKEN},
                    LLMResponse.model_construct(text={"private": OUTPUT}, finish_reason="stop")):
            with self.subTest(envelope_type=type(raw).__name__):
                self.assert_failure(self.generate(raw), "provider_envelope")

    def test_failure_contract_rejects_missing_or_incompatible_diagnostics(self):
        invalid = (
            {"code": "INVALID_RESPONSE", "message": "safe"},
            {"code": "TIMEOUT", "message": "safe", "invalid_response_stage": "provider_envelope"},
            {"code": "INVALID_RESPONSE", "message": "safe", "invalid_response_stage": "output_schema",
             "invalid_response_category": "bedrock_empty_output"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                LLMFailure.model_validate(value)
        valid = LLMFailure(code="INVALID_RESPONSE", message="safe",
                           invalid_response_stage="json_guardrail",
                           invalid_response_category="bedrock_empty_output")
        self.assertEqual((valid.invalid_response_stage, valid.invalid_response_category),
                         ("json_guardrail", "bedrock_empty_output"))

    def test_json_guardrail_rejection_preserves_ambiguity_and_resource_limits(self):
        for text in (OUTPUT, f'{{"summary":"{OUTPUT}","count":1,"count":2}}',
                     f'{{"summary":"{OUTPUT}","count":NaN}}',
                     f'{{"summary":"{OUTPUT}","count":1e999}}',
                     "[" * (MAX_DEPTH + 2) + '"' + OUTPUT + '"' + "]" * (MAX_DEPTH + 2),
                     " " * MAX_PAYLOAD_CHARS + OUTPUT):
            with self.subTest(size=len(text)):
                self.assert_failure(self.generate(self.response(text)), "json_guardrail")
                self.assertNotIn("invalid_response_category", self.observer.snapshot()["events"][-1])

    async def test_bedrock_empty_and_nonempty_json_failures_have_closed_categories(self):
        client = Mock()
        provider = BedrockProvider(model_id="test.model-v1", client=client)
        for text, category in (("", "bedrock_empty_output"),
                               (OUTPUT, "bedrock_nonempty_json_rejected")):
            with self.subTest(category=category):
                client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
                    "role": "assistant", "content": [{"text": text}]}}}
                result = StructuredLLM(provider).generate(self.request, Summary)
                self.assertEqual(result.code, "INVALID_RESPONSE")
                event = self.observer.snapshot()["events"][-1]
                self.assertEqual(event["provider"], "bedrock")
                self.assertEqual(event["invalid_response_stage"], "json_guardrail")
                self.assertEqual(event["invalid_response_category"], category)
                self.assertNotIn(OUTPUT, json.dumps(event))
                self.assertNotIn(PROMPT, json.dumps(event))
                self.assertNotIn(OUTPUT, result.model_dump_json())
        self.assertEqual(client.converse.call_count, 2)
        app = create_app(auth_provider=self.auth, observer=self.observer)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local.test") as http_client:
            response = await http_client.get("/api/diagnostics", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200)
        events = [event for event in response.json()["events"] if event["operation"] == "llm"]
        self.assertEqual([event["invalid_response_category"] for event in events],
                         ["bedrock_empty_output", "bedrock_nonempty_json_rejected"])
        self.assertNotIn(OUTPUT, response.text)
        self.assertNotIn(PROMPT, response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(OUTPUT, self.output.getvalue())

    def test_output_schema_rejection_preserves_strict_types_and_extra_field_policy(self):
        for value in ({"summary": OUTPUT}, {"summary": OUTPUT, "count": "1"},
                      {"summary": OUTPUT, "count": True}, {"summary": OUTPUT, "count": 1, TOKEN: OUTPUT},
                      [OUTPUT], None):
            with self.subTest(value_type=type(value).__name__):
                self.assert_failure(self.generate(self.response(json.dumps(value))), "output_schema")

    def test_nova_lite_stringified_arguments_report_only_closed_field_categories(self):
        client = Mock()
        client.converse.return_value = {"stopReason": "tool_use", "output": {"message": {
            "role": "assistant", "content": [{"toolUse": {"toolUseId": "tooluse_offline",
                "name": "submit_tool_decision", "input": {"decision": {"kind": "tool",
                    "name": "get_case", "arguments": json.dumps({"case_id": OUTPUT})}}}}]}},
            "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5}}
        provider = BedrockProvider(model_id="us.amazon.nova-lite-v1:0", client=client)
        result = StructuredLLM(provider).generate(self.request, AgentDecision)
        self.assert_failure(result, "output_schema")
        event = self.observer.snapshot()["events"][-1]
        self.assertEqual(event["provider"], "bedrock")
        self.assertEqual(event["output_schema_errors"],
                         [{"type": "dict_type", "location": ["decision", "tool", "arguments"]}])
        self.assertFalse(event["output_schema_errors_truncated"])
        self.assertNotIn(OUTPUT, self.output.getvalue())
        client.converse.assert_called_once()

    def test_success_and_other_failures_do_not_inherit_validation_stage(self):
        self.assert_failure(self.generate(self.response(json.dumps({"summary": OUTPUT}))), "output_schema")
        valid = json.dumps({"summary": OUTPUT, "count": 1})
        self.assertTrue(self.generate(self.response(valid)).ok)
        for reason, code in (("refusal", "REFUSED"), ("length", "INCOMPLETE")):
            self.assertEqual(self.generate(self.response(OUTPUT, reason)).code, code)
        for code in ("TIMEOUT", "UNAVAILABLE"):
            provider = Mock()
            provider.generate.side_effect = ProviderFailure(code)
            self.assertEqual(StructuredLLM(provider).generate(self.request, Summary).code, code)
            provider.generate.assert_called_once()
        provider = Mock()
        large = LLMRequest(messages=(LLMMessage(role="user", content="x" * (MAX_PROMPT_CHARS + 1)),))
        self.assertEqual(StructuredLLM(provider).generate(large, Summary).code, "INPUT_LIMIT")
        provider.generate.assert_not_called()
        provider.generate.side_effect = RuntimeError(OUTPUT)
        with self.assertRaisesRegex(RuntimeError, OUTPUT):
            StructuredLLM(provider).generate(self.request, Summary)
        self.assertTrue(all("invalid_response_stage" not in event
                            for event in self.observer.snapshot()["events"][1:]))
        self.assertTrue(all("output_schema_errors" not in event and "output_schema_errors_truncated" not in event
                            for event in self.observer.snapshot()["events"][1:]))
        self.assertNotIn(OUTPUT, self.output.getvalue())
        self.assertNotIn(PROMPT, self.output.getvalue())

    def test_stage_metadata_rejects_unknown_strings_and_objects(self):
        for value in (OUTPUT, TOKEN, "", None, 1, [], {"private": OUTPUT}, RuntimeError(OUTPUT)):
            with span("llm"):
                annotate(invalid_response_stage=value, provider_called=False, validation_errors=OUTPUT)
            event = self.observer.snapshot()["events"][-1]
            self.assertNotIn("invalid_response_stage", event)
            self.assertNotIn("validation_errors", event)
            self.assertFalse(event["provider_called"])
        self.assertNotIn(OUTPUT, self.output.getvalue())
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_category_metadata_rejects_unknown_strings_and_objects(self):
        for value in (OUTPUT, TOKEN, "", None, 1, [], {"private": OUTPUT}, RuntimeError(OUTPUT)):
            with span("llm"):
                annotate(invalid_response_category=value)
            self.assertNotIn("invalid_response_category", self.observer.snapshot()["events"][-1])
        self.assertNotIn(OUTPUT, self.output.getvalue())
        self.assertNotIn(TOKEN, self.output.getvalue())

    async def test_http_diagnostics_expose_only_fixed_stages_to_authenticated_reviewers(self):
        stages = ("provider_envelope", "json_guardrail", "output_schema")
        for raw, stage in zip((None, self.response(OUTPUT), self.response(json.dumps({"summary": OUTPUT}))), stages):
            self.assert_failure(self.generate(raw), stage)
        self.assertTrue(self.generate(self.response(json.dumps({"summary": OUTPUT, "count": 1}))).ok)
        many_errors = {"summary": OUTPUT, "count": 1,
                       **{TOKEN + str(index): OUTPUT for index in range(MAX_SCHEMA_ERRORS + 1)}}
        self.assert_failure(self.generate(self.response(json.dumps(many_errors))), "output_schema")
        local_events = self.observer.snapshot()["events"]

        async def fetch():
            app = create_app(auth_provider=self.auth, observer=self.observer)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
                denied = await client.get("/api/diagnostics")
                self.assertEqual(denied.status_code, 401)
                response = await client.get("/api/diagnostics", headers={"Authorization": f"Bearer {TOKEN}"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                return response.json()

        result = await fetch()
        DiagnosticsResponse.model_validate(result)
        events = [event for event in result["events"] if event["operation"] == "llm"]
        self.assertEqual([event["invalid_response_stage"] for event in events], [*stages, None, "output_schema"])
        self.assertEqual([event["error"] for event in events], ["INVALID_RESPONSE"] * 3 + [None, "INVALID_RESPONSE"])
        for event, local in zip(events, local_events):
            self.assertEqual(event["output_schema_errors"], local.get("output_schema_errors"))
            self.assertEqual(event["output_schema_errors_truncated"], local.get("output_schema_errors_truncated"))
        self.assertEqual(events[2]["output_schema_errors"], [{"type": "missing", "location": ["<field>"]}])
        self.assertFalse(events[2]["output_schema_errors_truncated"])
        self.assertEqual(len(events[-1]["output_schema_errors"]), MAX_SCHEMA_ERRORS)
        self.assertTrue(events[-1]["output_schema_errors_truncated"])
        for marker in (PROMPT, OUTPUT, TOKEN):
            self.assertNotIn(marker, json.dumps(result))

    def test_telemetry_contract_rejects_unknown_stages_and_extra_payload_fields(self):
        self.generate(self.response(OUTPUT))
        event = self.observer.snapshot()["events"][-1]
        for label in ("bedrock", "fake", "other", None):
            self.assertEqual(TelemetryEvent.model_validate({**event, "provider": label}).provider, label)
        for label in ("gemini", "PRIVATE"):
            with self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "provider": label})
        for value in (OUTPUT, "", "schema", 1, True, [], {"private": OUTPUT}):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "invalid_response_stage": value})
        for value in (OUTPUT, "", "schema", 1, True, [], {"private": OUTPUT}):
            with self.subTest(category_type=type(value).__name__), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "invalid_response_category": value})
        for category in ("bedrock_empty_output", "bedrock_nonempty_json_rejected"):
            self.assertEqual(TelemetryEvent.model_validate(
                {**event, "invalid_response_category": category}).invalid_response_category, category)
        for field in ("prompt", "model_output", "credentials", "validation_errors"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, field: OUTPUT})
        self.assertIsNone(TelemetryEvent.model_validate({**event, "invalid_response_stage": None}).invalid_response_stage)
        self.assertIsNone(TelemetryEvent.model_validate(
            {key: value for key, value in event.items() if key != "invalid_response_stage"}).invalid_response_stage)
        self.assertIsNone(TelemetryEvent.model_validate({**event, "invalid_response_category": None}).invalid_response_category)
        self.assertIsNone(TelemetryEvent.model_validate(event).invalid_response_category)

    def test_schema_error_contract_matches_sanitized_vocabulary_and_optional_defaults(self):
        self.assertEqual(set(get_args(OutputSchemaError.model_fields["type"].annotation)), SCHEMA_ERROR_TYPES | {"other"})
        labels = get_args(get_args(OutputSchemaError.model_fields["location"].annotation)[0])
        self.assertEqual(set(labels), SCHEMA_LOCATION_LABELS | {"<field>", "<index>", "<unknown>", "<truncated>"})
        for code in SCHEMA_ERROR_TYPES | {"other"}:
            for label in labels:
                shape = {"type": code, "location": [label] * MAX_SCHEMA_LOCATION}
                self.assertEqual(OutputSchemaError.model_validate(shape).model_dump(mode="json"), shape)
        self.generate(self.response(OUTPUT))
        event = self.observer.snapshot()["events"][-1]
        parsed = TelemetryEvent.model_validate(event)
        self.assertIsNone(parsed.output_schema_errors)
        self.assertIsNone(parsed.output_schema_errors_truncated)
        parsed = TelemetryEvent.model_validate({**event, "output_schema_errors": [], "output_schema_errors_truncated": False})
        self.assertEqual(parsed.model_dump(mode="json")["output_schema_errors"], [])
        shape = {"type": "other", "location": []}
        parsed = TelemetryEvent.model_validate({**event, "output_schema_errors": [shape] * MAX_SCHEMA_ERRORS,
                                              "output_schema_errors_truncated": True})
        self.assertEqual(parsed.model_dump(mode="json")["output_schema_errors"], [shape] * MAX_SCHEMA_ERRORS)

    def test_schema_error_contract_rejects_raw_fields_unknown_values_and_excess_size(self):
        self.generate(self.response(OUTPUT))
        event = self.observer.snapshot()["events"][-1]
        shape = {"type": "missing", "location": ["decision"]}
        invalid = [{**shape, "type": TOKEN}, {**shape, "location": [OUTPUT]},
                   {**shape, "location": [123]}, {**shape, "location": ["decision"] * (MAX_SCHEMA_LOCATION + 1)},
                   {"type": "missing"}, {"location": []}]
        invalid.extend({**shape, field: OUTPUT} for field in
                       ("input", "msg", "ctx", "url", "prompt", "model_output", "customer_data", "credentials"))
        for value in invalid:
            with self.subTest(keys=list(value)), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "output_schema_errors": [value]})
        for value in ([shape] * (MAX_SCHEMA_ERRORS + 1), OUTPUT, {"errors": [shape]}):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "output_schema_errors": value})
        for value in ("true", "false", 0, 1, OUTPUT):
            with self.subTest(flag_type=type(value).__name__), self.assertRaises(ValidationError):
                TelemetryEvent.model_validate({**event, "output_schema_errors_truncated": value})

    def test_failed_logging_does_not_change_failure_or_retry_provider(self):
        with patch.object(LOGGER, "info", side_effect=RuntimeError(TOKEN)):
            result = self.generate(self.response(OUTPUT))
        self.assertEqual(result.model_dump(), {"ok": False, "code": "INVALID_RESPONSE",
                         "message": "Output does not match the requested JSON model",
                         "invalid_response_stage": "json_guardrail"})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["logging_failures"], 1)
        self.assertEqual(snapshot["events"][0]["invalid_response_stage"], "json_guardrail")
        self.assertEqual(snapshot["metrics"]["llm"]["provider_calls"], 1)
        self.assertNotIn(TOKEN, json.dumps(snapshot))

    def test_schema_shapes_identify_missing_tag_type_and_extra_field_failures(self):
        cases = (
            ({}, "missing", ["decision"]),
            ({"decision": {}}, "union_tag_not_found", ["decision"]),
            ({"decision": {"kind": OUTPUT}}, "union_tag_invalid", ["decision"]),
            ({"decision": {"kind": "tool", "name": "fixture"}}, "missing", ["decision", "tool", "arguments"]),
            ({"decision": {"kind": "tool", "name": 123, "arguments": {}}},
             "string_type", ["decision", "tool", "name"]),
            ({"decision": {"kind": "final", TOKEN: OUTPUT}}, "extra_forbidden", ["decision", "final", "<field>"]),
        )
        for value, code, location in cases:
            with self.subTest(code=code):
                result = self.generate(self.response(json.dumps(value)), AgentDecision)
                self.assert_failure(result, "output_schema")
                event = self.observer.snapshot()["events"][-1]
                self.assertEqual(event["output_schema_errors"], [{"type": code, "location": location}])
                self.assertFalse(event["output_schema_errors_truncated"])

    def test_nested_schema_locations_redact_dictionary_keys_and_list_indexes(self):
        class Nested(BaseModel):
            model_config = ConfigDict(extra="forbid")
            items: list[dict[str, int]]

        result = self.generate(self.response(json.dumps({"items": [{TOKEN: OUTPUT}]})), Nested)
        self.assert_failure(result, "output_schema")
        self.assertEqual(self.observer.snapshot()["events"][-1]["output_schema_errors"],
                         [{"type": "int_type", "location": ["<field>", "<index>", "<field>"]}])

    def test_custom_validation_codes_messages_and_context_are_never_recorded(self):
        class Custom(BaseModel):
            model_config = ConfigDict(extra="forbid")
            summary: str

            @field_validator("summary")
            @classmethod
            def reject(cls, value):
                raise PydanticCustomError(TOKEN, OUTPUT + " {private}", {"private": PROMPT})

        result = self.generate(self.response(json.dumps({"summary": OUTPUT})), Custom)
        self.assert_failure(result, "output_schema")
        self.assertEqual(self.observer.snapshot()["events"][-1]["output_schema_errors"],
                         [{"type": "other", "location": ["<field>"]}])

    def test_schema_diagnostics_bound_error_count_and_location_depth(self):
        payload = {"summary": OUTPUT, "count": 1,
                   **{TOKEN + str(index): OUTPUT for index in range(MAX_SCHEMA_ERRORS + 3)}}
        self.assert_failure(self.generate(self.response(json.dumps(payload))), "output_schema")
        event = self.observer.snapshot()["events"][-1]
        self.assertEqual(event["output_schema_errors"],
                         [{"type": "extra_forbidden", "location": ["<field>"]}] * MAX_SCHEMA_ERRORS)
        self.assertTrue(event["output_schema_errors_truncated"])
        # Exercise the closed metadata boundary separately, including fields that
        # Pydantic/custom validators could otherwise use to carry private strings.
        with span("llm"):
            annotate(output_schema_errors=[{"type": TOKEN, "location": [OUTPUT] * (MAX_SCHEMA_LOCATION + 1),
                                            "msg": PROMPT, "input": OUTPUT, "ctx": {"secret": TOKEN}}]
                     * (MAX_SCHEMA_ERRORS + 1))
        shapes = self.observer.snapshot()["events"][-1]["output_schema_errors"]
        self.assertEqual(shapes, [{"type": "other", "location":
                                  ["<field>"] * (MAX_SCHEMA_LOCATION - 1) + ["<truncated>"]}] * MAX_SCHEMA_ERRORS)
        for marker in (PROMPT, OUTPUT, TOKEN):
            self.assertNotIn(marker, self.output.getvalue())

    def test_schema_diagnostic_extraction_failures_do_not_change_validation_results(self):
        broken = Mock()
        broken.errors.side_effect = RuntimeError(TOKEN)
        with span("llm"):
            annotate_schema_validation(broken)
        broken.errors.assert_called_once_with(include_url=False, include_context=False, include_input=False)
        self.assertNotIn("output_schema_errors", self.observer.snapshot()["events"][-1])
        with patch("backend.app.observability.annotate", side_effect=RuntimeError(TOKEN)):
            result = self.generate(self.response(json.dumps({"summary": OUTPUT})))
        self.assert_failure(result, "output_schema")
        self.assertNotIn("output_schema_errors", self.observer.snapshot()["events"][-1])

    def test_envelope_and_json_failures_do_not_get_schema_error_shapes(self):
        self.assert_failure(self.generate(None), "provider_envelope")
        self.assert_failure(self.generate(self.response(OUTPUT)), "json_guardrail")
        for event in self.observer.snapshot()["events"]:
            self.assertNotIn("output_schema_errors", event)
            self.assertNotIn("output_schema_errors_truncated", event)
