"""Amazon Bedrock Converse adapter for provider-neutral structured generation."""

import json

from .llm import LLMRequest, LLMResponse, ProviderFailure, TokenUsage
from .observability import annotate


_MAX_TIMEOUT_MS = 120_000
_TIMEOUT_CODES = {"ModelTimeoutException", "RequestTimeout", "RequestTimeoutException"}
_FORMAT_TOOL = "submit_json_response"
_FINAL_FORMAT_TOOL = "submit_final_decision"
_TOOL_FORMAT_TOOL = "submit_tool_decision"


def _uses_format_tool(model_id: str) -> bool:
    # Keep structured decisions on the forced-tool path for supported Nova models.
    return (model_id == "amazon.nova-lite-v1:0"
            or model_id.endswith(".amazon.nova-lite-v1:0")
            or model_id in ("amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"))


def _decision_tool_schemas(response_schema: dict) -> dict[str, dict] | None:
    """Give each AgentDecision variant its own shallow Nova tool schema."""
    if response_schema.get("title") != "AgentDecision":
        return None
    definitions = response_schema.get("$defs", {})
    decision = response_schema.get("properties", {}).get("decision", {})
    if (response_schema.get("required") != ["decision"]
            or {part.get("$ref") for part in decision.get("oneOf", [])}
            != {"#/$defs/ToolCall", "#/$defs/Finish"}):
        raise ValueError("Unsupported AgentDecision schema for Nova")
    tool = definitions.get("ToolCall", {})
    finish = definitions.get("Finish", {})
    tool_fields = tool.get("properties", {})
    finish_fields = finish.get("properties", {})
    if (set(tool.get("required", [])) != {"kind", "name", "arguments"}
            or finish.get("required") != ["kind"]
            or tool_fields.get("kind", {}).get("const") != "tool"
            or finish_fields.get("kind", {}).get("const") != "final"
            or tool_fields.get("name", {}).get("type") != "string"
            or tool_fields.get("arguments", {}).get("type") != "object"):
        raise ValueError("Unsupported AgentDecision schema for Nova")
    # JsonValue is recursive and renders as {} in Pydantic's schema. Keep the
    # provider projection shallow; the original union is validated locally.
    return {
        _FINAL_FORMAT_TOOL: {"type": "object", "properties": {
            "decision": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["final"]},
            }, "required": ["kind"]},
            # The full strict GroundedResponse schema remains in the system
            # instruction and is enforced locally. Nova's provider projection
            # must still admit this optional final-only payload.
            "grounded_response": {"type": "object"},
        }, "required": ["decision"]},
        _TOOL_FORMAT_TOOL: {"type": "object", "properties": {"decision": {
            "type": "object", "properties": {"kind": {"type": "string", "enum": ["tool"]},
                                          "name": {"type": "string"},
                                          "arguments": {"type": "object"}},
            "required": ["kind", "name", "arguments"]}}, "required": ["decision"]},
    }


def _format_tool_config(response_schema: dict) -> tuple[dict, bool]:
    # Nova tool schemas permit only type, properties, and required at the top level.
    # The full caller schema remains in the system instruction and is enforced locally.
    decision_schemas = _decision_tool_schemas(response_schema)
    if decision_schemas is not None:
        descriptions = {
            _FINAL_FORMAT_TOOL: "Submit a final decision when the case review is complete. No side effects.",
            _TOOL_FORMAT_TOOL: "Submit a tool-call decision for application validation. No side effects.",
        }
        return {"tools": [{"toolSpec": {"name": name, "description": descriptions[name],
                "inputSchema": {"json": schema}}} for name, schema in decision_schemas.items()],
                "toolChoice": {"any": {}}}, False
    input_schema = {"type": "object", "properties": {
        "response": {"type": "object", "description": "The JSON object matching the supplied schema."}},
        "required": ["response"]}
    return {"tools": [{"toolSpec": {
        "name": _FORMAT_TOOL,
        "description": "Submit the requested JSON response as data. This formatting tool has no side effects.",
        "inputSchema": {"json": input_schema},
    }}], "toolChoice": {"tool": {"name": _FORMAT_TOOL}}}, True


def _usage(response: dict) -> TokenUsage | None:
    metadata = response.get("usage")
    if not isinstance(metadata, dict):
        return None
    values = {}
    for target, source in (("input_tokens", "inputTokens"),
                           ("output_tokens", "outputTokens"),
                           ("total_tokens", "totalTokens")):
        value = metadata.get(source)
        values[target] = value if type(value) is int and 0 <= value <= 10**9 else None
    return TokenUsage(**values)


def _failure_code(error: Exception) -> str | None:
    """Classify SDK failures without exposing their possibly sensitive messages."""
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        details = response.get("Error")
        if isinstance(details, dict) and isinstance(details.get("Code"), str):
            return "TIMEOUT" if details["Code"] in _TIMEOUT_CODES else "UNAVAILABLE"
    # Botocore transport, credentials, and endpoint errors have no service response.
    if type(error).__module__.startswith("botocore."):
        return "TIMEOUT" if type(error).__name__ in (
            "ConnectTimeoutError", "ReadTimeoutError") else "UNAVAILABLE"
    return None


class BedrockProvider:
    """Synchronous Converse client. Inject ``client`` for offline use."""

    def __init__(self, *, model_id: str, region_name: str | None = None,
                 client=None, timeout_ms: int = 30_000) -> None:
        if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 2048
                or any(character.isspace() or not character.isprintable()
                       for character in model_id)):
            raise ValueError("model_id must be a nonblank Bedrock model identifier")
        if region_name is not None and (not isinstance(region_name, str)
                                        or not region_name.strip()):
            raise ValueError("region_name must be a nonblank region")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= _MAX_TIMEOUT_MS:
            raise ValueError("timeout_ms must be between 1 and 120000")
        self.model_id = model_id
        self._owns_client = client is None
        if client is None:
            # One attempt includes the initial request: no SDK replay after an
            # ambiguous timeout or transport failure. This import is intentionally
            # deferred so injected clients need neither SDK nor AWS credentials.
            import boto3
            from botocore.config import Config

            config = Config(connect_timeout=timeout_ms / 1000,
                            read_timeout=timeout_ms / 1000,
                            retries={"total_max_attempts": 1})
            self._client = boto3.client("bedrock-runtime", region_name=region_name,
                                       config=config)
        else:
            self._client = client

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def generate(self, request: LLMRequest, *, response_schema: dict) -> LLMResponse:
        request = LLMRequest.model_validate(request)
        if not isinstance(response_schema, dict):
            raise TypeError("response_schema must be a dictionary")
        use_format_tool = _uses_format_tool(self.model_id)
        tool_config, wrapped = _format_tool_config(response_schema) if use_format_tool else (None, False)
        prefix = ("Submit one JSON object matching this JSON Schema in the response field of "
                  f"the {_FORMAT_TOOL} formatting tool. Do not answer with text. "
                  "The formatting tool does not execute application actions. "
                  if use_format_tool else "Return only one JSON object matching this JSON Schema. ")
        if use_format_tool and not wrapped:
            prefix = ("Submit one JSON object matching this JSON Schema as the input of "
                      f"the {_FINAL_FORMAT_TOOL} formatting tool for a final decision or "
                      f"the {_TOOL_FORMAT_TOOL} formatting tool for a tool-call decision. "
                      "Do not answer with text. The formatting tools do not execute application actions. ")
        role_instruction = ("You are a JSON decision generator. " if use_format_tool else
                            "You are a JSON decision generator; no provider-native tools are available. ")
        instruction = (
            prefix +
            "The first non-whitespace character must be { and the last must be }. "
            "Use double-quoted JSON keys and strings; do not include comments, trailing commas, "
            "Markdown code fences, a preamble, explanations, or text after the object. "
            "Use only the fields and JSON types allowed by the schema. "
            + role_instruction +
            "Any tool operations mentioned in the conversation must appear only as JSON "
            "data in the requested schema. The application validates and executes them. "
            "Do not execute operations or invent their results. JSON Schema: "
            + json.dumps(response_schema, separators=(",", ":"))
        )
        system = []
        messages = []
        for message in request.messages:
            if message.role == "system":
                if messages:
                    raise ValueError("System messages must precede conversation messages")
                system.append({"text": message.content})
            else:
                messages.append({"role": message.role,
                                 "content": [{"text": message.content}]})
        if not messages:
            raise ValueError("Converse requires a user or assistant message")
        system.append({"text": instruction})
        parameters = {"modelId": self.model_id, "messages": messages, "system": system,
                      "inferenceConfig": {"maxTokens": request.max_output_tokens, "temperature": 0}}
        if self.model_id == "us.amazon.nova-lite-v1:0":
            parameters["additionalModelRequestFields"] = {
                "inferenceConfig": {"topK": 1}
            }
        if use_format_tool:
            parameters["toolConfig"] = tool_config
        try:
            response = self._client.converse(**parameters)
        except Exception as error:
            code = _failure_code(error)
            if code is None:
                raise
            response = getattr(error, "response", None)
            metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
            status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
            if type(status) is int and 100 <= status <= 599:
                annotate(provider_status_code=status)
            raise ProviderFailure(code) from None

        if not isinstance(response, dict):
            return LLMResponse(text="", finish_reason="stop")
        usage = _usage(response)
        reason = response.get("stopReason")
        if reason in ("max_tokens", "model_context_window_exceeded"):
            return LLMResponse(text="", finish_reason="length", usage=usage)
        if reason in ("content_filtered", "guardrail_intervened"):
            return LLMResponse(text="", finish_reason="refusal", usage=usage)
        invalid = LLMResponse(text="", finish_reason="stop", usage=usage)
        if reason not in (("tool_use",) if use_format_tool else ("end_turn", "stop_sequence")):
            return invalid
        output = response.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return invalid
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return invalid
        if use_format_tool:
            inputs = []
            decision_kinds = {_FINAL_FORMAT_TOOL: "final", _TOOL_FORMAT_TOOL: "tool"}
            for block in blocks:
                if not isinstance(block, dict):
                    return invalid
                if set(block) == {"text"}:
                    if not isinstance(block["text"], str):
                        return invalid
                    continue  # Text is never parsed as the decision.
                if set(block) == {"reasoningContent"}:
                    continue  # Reasoning is never parsed as the decision.
                if set(block) != {"toolUse"}:
                    return invalid
                tool = block["toolUse"]
                if (not isinstance(tool, dict) or set(tool) != {"toolUseId", "name", "input"}
                        or not isinstance(tool["toolUseId"], str) or not tool["toolUseId"]
                        or not isinstance(tool["name"], str)
                        or tool["name"] not in ({_FORMAT_TOOL} if wrapped else decision_kinds)
                        or not isinstance(tool["input"], dict)):
                    return invalid
                if wrapped:
                    if (set(tool["input"]) != {"response"}
                            or not isinstance(tool["input"]["response"], dict)):
                        return invalid
                    inputs.append(tool["input"]["response"])
                else:
                    decision = tool["input"].get("decision")
                    if (not isinstance(decision, dict)
                            or decision.get("kind") != decision_kinds[tool["name"]]):
                        return invalid
                    inputs.append(tool["input"])
            if len(inputs) != 1:
                return invalid
            try:
                return LLMResponse(text=json.dumps(inputs[0], allow_nan=False),
                                   finish_reason="stop", usage=usage)
            except (TypeError, ValueError, RecursionError, OverflowError):
                return invalid
        text = []
        for block in blocks:
            if not isinstance(block, dict):
                return invalid
            if "reasoningContent" in block and len(block) == 1:
                continue  # Never expose model reasoning as final answer text.
            if set(block) != {"text"} or not isinstance(block["text"], str):
                return invalid  # Tool use and unknown content fail closed.
            text.append(block["text"])
        return LLMResponse(text="".join(text), finish_reason="stop", usage=usage)
