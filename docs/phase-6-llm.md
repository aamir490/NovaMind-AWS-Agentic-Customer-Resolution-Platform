# Phase 6 — Local LLM foundation

`backend/app/llm.py` is a standalone module using existing Pydantic and standard-library types. It imports no domain, business service, tool, or API code. Nothing calls it from the running application. No real model provider is connected.

## Contracts

- `LLMRequest` contains a nonempty sequence of typed system/user/assistant messages and a positive bounded output-token limit. Messages must contain nonblank text. Unknown fields are rejected.
- `LLMResponse` contains provider text and an explicit `stop`, `length`, or `refusal` finish reason.
- `LLMProvider` is a Python protocol: `generate(request, response_schema=...)` returns `LLMResponse`. Future adapters must map provider-specific output and expected transport failures into these contracts.
- `StructuredLLM.generate(request, output_model)` sends the caller's Pydantic model schema to the provider, then validates the completed JSON against that model with strict validation. Success contains `ok=True` and typed `data`; failure contains `ok=False`, a code, and a safe message. Output field constraints and extra-field policy belong to the caller's output model; use `extra="forbid"` to reject unknown output fields.
- `FakeLLMProvider` returns the same configured response or expected failure on each call. It ignores prompt content and schema, performs no inference or I/O, and exists for tests only. The wrapper still validates its output.

Example, using a test fixture:

```python
from pydantic import BaseModel, ConfigDict
from backend.app.llm import FakeLLMProvider, LLMMessage, LLMRequest, LLMResponse, StructuredLLM

class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str

client = StructuredLLM(FakeLLMProvider(LLMResponse(
    text='{"summary":"Configured fixture only"}', finish_reason="stop",
)))
result = client.generate(
    LLMRequest(messages=(LLMMessage(role="user", content="Summarize this fixture"),)),
    Summary,
)
print(result.model_dump(mode="json"))
```

## Explicit failures

`ProviderFailure` maps expected adapter failures to `TIMEOUT` or `UNAVAILABLE`. Refusal and token-limit completion become `REFUSED` and `INCOMPLETE`, even if their text happens to be valid JSON. Invalid envelopes, malformed JSON, and schema mismatches become `INVALID_RESPONSE`. No raw prompt, output, or provider error details are included in failure messages.

Invalid caller requests raise Pydantic validation errors before provider invocation. Unexpected adapter/programming exceptions propagate; they are not disguised as ordinary generation failures. There are no automatic retries, JSON repair, or fallback answers.

## Boundaries and limitations

This is a synchronous, structured-JSON-only foundation, without streaming, tool calling, agents, or a real provider. Token limits are adapter instructions; the fake does not simulate token counts. Actual timeouts, provider schema compatibility, usage accounting, and network behavior are not implemented or verified.

Schema validity does not prove truth or business eligibility. Generated data has no authorization authority. The module has no business service handles and cannot approve/reject proposals or execute actions. Existing deterministic rules and the human review boundary are unchanged. No SDK, package, AWS, authentication, database, infrastructure, or frontend change was introduced.

## Focused verification

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_llm.py -v`

**10 tests passed**: request validation, pre-dispatch rejection, provider/schema handoff, typed JSON success, malformed/invalid outputs, refusal/truncation, expected failures, invalid provider envelopes, unexpected defects, and repeatable independent fake results. Tests used no external services. Full regression, frontend build, and browser verification were intentionally not run. Phase 7 has not started.
