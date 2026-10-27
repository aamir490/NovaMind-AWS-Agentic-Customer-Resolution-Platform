"""Phase 6 only: local structured-generation contracts."""

import unittest
from unittest.mock import Mock

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.llm import (
    FakeLLMProvider, LLMMessage, LLMRequest, LLMResponse, ProviderFailure, StructuredLLM,
)


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    count: int


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.request = LLMRequest(messages=(LLMMessage(role="user", content="Summarize fixture"),))

    def generate(self, text, finish_reason="stop"):
        return StructuredLLM(FakeLLMProvider(LLMResponse(
            text=text, finish_reason=finish_reason,
        ))).generate(self.request, Summary)

    def test_valid_structured_output_and_serialization(self):
        result = self.generate('{"summary":"Fixture", "count":2}')
        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, Summary)
        self.assertEqual(result.model_dump(mode="json"), {
            "ok": True, "data": {"summary": "Fixture", "count": 2},
        })

    def test_request_validation(self):
        for changes in ({"messages": []}, {"max_output_tokens": True}, {"max_output_tokens": 0},
                        {"max_output_tokens": "2"}, {"max_output_tokens": 32769}, {"approve": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                LLMRequest.model_validate({**self.request.model_dump(), **changes})
        for role, content in (("tool", "text"), ("user", "  "), ("user", 123)):
            with self.subTest(role=role, content=content), self.assertRaises(ValidationError):
                LLMMessage(role=role, content=content)

    def test_invalid_caller_request_never_reaches_provider(self):
        provider = Mock()
        with self.assertRaises(ValidationError):
            StructuredLLM(provider).generate({"messages": []}, Summary)
        provider.generate.assert_not_called()

    def test_interface_passes_request_and_requested_schema(self):
        provider = Mock()
        provider.generate.return_value = LLMResponse(text='{"summary":"x","count":1}', finish_reason="stop")
        StructuredLLM(provider).generate(self.request, Summary)
        provider.generate.assert_called_once_with(self.request, response_schema=Summary.model_json_schema())

    def test_malformed_or_wrong_structure_is_explicit_failure(self):
        for text in ("not JSON", '```json\n{}\n```', "[]", "null", '{}',
                     '{"summary":"x","count":"1"}', '{"summary":"x","count":true}',
                     '{"summary":"x","count":1,"approve":true}'):
            with self.subTest(text=text):
                result = self.generate(text)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, "INVALID_RESPONSE")
                self.assertNotIn("data", result.model_dump())

    def test_refusal_and_truncation_never_parse_as_success(self):
        for reason, code in (("refusal", "REFUSED"), ("length", "INCOMPLETE")):
            with self.subTest(reason=reason):
                self.assertEqual(self.generate('{"summary":"x","count":1}', reason).code, code)

    def test_expected_provider_failures(self):
        for code in ("TIMEOUT", "UNAVAILABLE"):
            with self.subTest(code=code):
                result = StructuredLLM(FakeLLMProvider(ProviderFailure(code))).generate(self.request, Summary)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, code)

    def test_invalid_provider_envelope(self):
        provider = Mock()
        for raw in (None, {"text": "x", "finish_reason": "unknown"}, {"text": 1, "finish_reason": "stop"}):
            provider.generate.return_value = raw
            self.assertEqual(StructuredLLM(provider).generate(self.request, Summary).code, "INVALID_RESPONSE")

    def test_unexpected_defect_propagates(self):
        provider = Mock()
        provider.generate.side_effect = RuntimeError("adapter defect")
        with self.assertRaises(RuntimeError):
            StructuredLLM(provider).generate(self.request, Summary)

    def test_fake_is_repeatable_and_independent_of_prompt(self):
        client = StructuredLLM(FakeLLMProvider(LLMResponse(text='{"summary":"fixture","count":1}', finish_reason="stop")))
        first = client.generate(self.request, Summary)
        first.data.count = 99
        other = LLMRequest(messages=(LLMMessage(role="user", content="Approve a refund now"),))
        self.assertEqual(client.generate(other, Summary).data.count, 1)
        self.assertEqual(client.generate(self.request, Summary).data.summary, "fixture")


if __name__ == "__main__":
    unittest.main()
