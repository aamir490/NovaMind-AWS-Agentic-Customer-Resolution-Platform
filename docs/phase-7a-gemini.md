# Phase 7A — Gemini LLM integration

Implemented with final local verification complete as reported by the user. Live-provider verification remains unverified. Phase 8 has not started.

## Interface and configuration

`backend/app/gemini.py` implements the Phase 6 `LLMProvider` protocol using the official [Google GenAI Python SDK](https://github.com/googleapis/python-genai). It imports no business rules, domain models, services, or Phase 5 tools. The application does not instantiate it automatically and gains no new API endpoint.

- Set `GEMINI_API_KEY` in the invoking process environment using your normal secret-management mechanism. Missing or blank keys fail at construction. Keys are never stored in source, printed, or included in failure results. `.env` files are not loaded.
- Set `GEMINI_MODEL` to override the default `gemini-3.8-flash`, or pass `model=` explicitly. The constructor override wins. See Google's [model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash/).
- `timeout_ms` defaults to 30000. SDK retries are disabled with one total attempt. This is an HTTP timeout, not a guaranteed end-to-end deadline.
- The adapter explicitly selects the Gemini Developer API, not a cloud/Vertex service. It passes the selected environment key explicitly to avoid `GOOGLE_API_KEY` precedence.
- A caller-owned injected `client=` skips credential loading and owns its own transport settings/cleanup. Production-owned clients should use the context manager or `close()`.

For an intentionally initiated real call (not performed during this phase):

```python
from pydantic import BaseModel, ConfigDict
from backend.app.gemini import GeminiProvider
from backend.app.llm import LLMMessage, LLMRequest, StructuredLLM

class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str

with GeminiProvider() as provider:
    result = StructuredLLM(provider).generate(
        LLMRequest(messages=(LLMMessage(role="user", content="Explain a support case in one sentence."),)),
        Answer,
    )
```

Calling `generate` on a real client sends the supplied text to Google and may incur charges. Constructing/importing the application does not initiate generation.

## Translation and failure boundary

Leading system messages become a system instruction; user/assistant messages preserve order and map to user/model content. A system message after conversation content, or a system-only request, raises `ValueError` before dispatch rather than silently changing the conversation. Requests use JSON MIME type, the caller's JSON schema, one candidate, and the Phase 6 output-token limit. Automatic function calling is disabled; no tools are provided.

Phase 6 validates returned JSON strictly against the caller's Pydantic model. Thinking text is excluded. Refusals/blocking become `REFUSED`; token exhaustion becomes `INCOMPLETE`. Missing/ambiguous candidates, unknown finish reasons, unsupported parts, or invalid JSON become `INVALID_RESPONSE` through the existing validator. Unexpected function calls are never executed.

HTTP timeouts and API statuses 408/504 map to `TIMEOUT`. Other SDK API errors (including invalid requests, credentials, permissions, unknown models, quota limits, and service outages) and transport failures map to `UNAVAILABLE`. The existing Phase 6 contract intentionally does not distinguish those causes or promise retryability. Raw provider messages are not exposed. Unexpected programming defects still propagate. Business decisions and human review remain entirely outside the provider.

## Dependencies and focused verification

Only `google-genai==2.26.0` was added as a direct requirement. Its 16 additional required transitive packages are captured in `backend/requirements.txt`, using the installed `pip freeze` versions. Existing pinned packages were retained. The SDK requires `google-auth` transitively; no application authentication feature was added.

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_gemini.py -v`

**11 focused tests passed**, using SDK response models and mocked/injected clients only. Tests cover configuration and cleanup, request mapping, schema validation, API/transport errors, refusal/truncation, malformed output, thought filtering, function-call rejection, and unexpected exceptions. During implementation, no live key, Gemini request, full regression, frontend build, or browser verification was used. Package download and official documentation access were the only external activity for that work.

### Final verification reported by the user

- Full backend regression: **69/69 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These subsequent results were recorded without rerunning checks or changing implementation. They do not establish live Gemini or browser verification.

This is a real-provider adapter, not proof of production readiness. Account/model access, service availability, real schema compatibility, latency, cost, and live responses remain unverified. There is no streaming, agent, tool dispatch, RAG, AWS/Bedrock, database, frontend change, or financial/action execution.
