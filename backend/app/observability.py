"""Bounded local diagnostics. Metadata only; never authorization or business state."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import json
import logging
from threading import RLock
from time import perf_counter
from uuid import UUID, uuid4


LOGGER = logging.getLogger("novamind.telemetry")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False  # The trusted host may attach a local handler explicitly.

OPERATIONS = frozenset({"http", "agent.loop", "agent.graph", "tool", "llm", "hitl.start",
                        "hitl.resume", "proposal.create", "proposal.review", "eligibility", "refund_eligibility"})
TOOLS = frozenset({"get_customer", "get_order", "get_case", "get_inventory", "get_policy",
                   "assess_eligibility", "assess_refund_eligibility", "create_resolution_proposal",
                   "get_proposal_status", "search_knowledge"})
ERRORS = frozenset({"UNAUTHENTICATED", "FORBIDDEN", "UNKNOWN_TOOL", "INVALID_INPUT", "NOT_FOUND", "CONFLICT",
    "KNOWLEDGE_UNAVAILABLE", "UNSAFE_CONTENT", "PAYLOAD_LIMIT", "INVALID_OUTPUT", "TIMEOUT", "UNAVAILABLE",
    "REFUSED", "INCOMPLETE", "INVALID_RESPONSE", "INPUT_LIMIT", "TOOL_ERROR", "LLM_ERROR", "STEP_LIMIT",
    "PROPOSAL_LIMIT", "CASE_MISMATCH", "INVALID_PROPOSAL_STATUS", "GRAPH_LIMIT", "GRAPH_ERROR", "WORKFLOW_ERROR",
    "REPEATED_TOOL_CALL", "EARLY_DETERMINISTIC_FALLBACK",
    "UNKNOWN_WORKFLOW", "NOT_PAUSED", "REVIEW_MISMATCH", "ALREADY_REVIEWED", "REVIEW_CONFLICT",
    "CONVERSATION_WRITE_FAILED", "CONVERSATION_UNAVAILABLE", "CONVERSATION_NOT_FOUND", "CONVERSATION_CASE_MISMATCH"})
STATUSES = frozenset({"INFORMATIONAL", "HUMAN_REVIEW_REQUIRED", "FAILED", "REVIEW_REQUIRED", "REVIEWED",
                      "COMPLETED", "PENDING_REVIEW", "APPROVED", "REJECTED"})
# Never derive these vocabularies from provider output, schema titles, or errors.
SCHEMA_ERROR_TYPES = frozenset({"missing", "extra_forbidden", "union_tag_invalid", "union_tag_not_found",
    "literal_error", "string_type", "int_type", "float_type", "bool_type", "dict_type", "list_type",
    "tuple_type", "model_type", "model_attributes_type", "json_type", "enum", "uuid_type", "uuid_parsing",
    "string_too_short", "string_too_long", "string_pattern_mismatch", "greater_than", "greater_than_equal",
    "less_than", "less_than_equal", "too_short", "too_long", "finite_number", "value_error", "assertion_error"})
SCHEMA_LOCATION_LABELS = frozenset({"decision", "kind", "name", "arguments", "tool", "final"})
MAX_SCHEMA_ERRORS = 8
MAX_SCHEMA_LOCATION = 8
_observer = ContextVar("local_observer", default=None)
_span = ContextVar("local_span", default=None)


def _error(value):
    return value if isinstance(value, str) and value in ERRORS else "ERROR"


def _schema_errors(value):
    """Closed, bounded error shapes; keys, indexes, custom codes and text are untrusted."""
    if type(value) not in (list, tuple):
        return []
    safe = []
    for item in value[:MAX_SCHEMA_ERRORS]:
        if type(item) is not dict:
            continue
        code = item.get("type")
        location = item.get("location")
        path = []
        if type(location) in (list, tuple):
            for part in location[:MAX_SCHEMA_LOCATION]:
                if type(part) is str:
                    path.append(part if part in SCHEMA_LOCATION_LABELS else "<field>")
                else:
                    path.append("<index>" if type(part) is int else "<unknown>")
            if len(location) > MAX_SCHEMA_LOCATION:
                path[-1] = "<truncated>"
        else:
            path = ["<unknown>"]
        safe.append({"type": code if type(code) is str and code in SCHEMA_ERROR_TYPES else "other",
                     "location": path})
    return safe


def annotate_schema_validation(error):
    """Best-effort local diagnostics; never stringify or retain a validation exception."""
    try:
        details = error.errors(include_url=False, include_context=False, include_input=False)
        annotate(output_schema_errors=[{"type": item.get("type"), "location": item.get("loc")}
                                       for item in details[:MAX_SCHEMA_ERRORS]],
                 output_schema_errors_truncated=len(details) > MAX_SCHEMA_ERRORS)
    except Exception:
        pass  # Diagnostic extraction must not replace the original safe failure.


def _metadata(fields):
    """Closed schema: never serialize generic objects, payloads, or exception text."""
    safe = {}
    for key, value in fields.items():
        if key in {"workflow_id", "case_id", "proposal_id", "reviewer_user_id"} and isinstance(value, UUID):
            safe[key] = str(value)
        elif key == "tool":
            safe[key] = value if isinstance(value, str) and value in TOOLS else "unknown"
        elif key == "status" and isinstance(value, str) and value in STATUSES:
            safe[key] = str(value)
        elif key == "provider" and value in {"bedrock", "fake", "other"}:
            safe[key] = value
        elif key == "invalid_response_stage" and isinstance(value, str) and value in {
                "provider_envelope", "json_guardrail", "output_schema"}:
            # Internal fixed labels only; never include validation errors or inputs.
            safe[key] = value
        elif key == "invalid_response_category" and type(value) is str and value in {
                "bedrock_empty_output", "bedrock_nonempty_json_rejected"}:
            safe[key] = value
        elif key == "output_schema_errors":
            safe[key] = _schema_errors(value)
        elif key == "output_schema_errors_truncated" and type(value) is bool:
            safe[key] = value
        elif key == "method" and value in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            safe[key] = value
        elif key in {"status_code", "provider_status_code"} and type(value) is int and 100 <= value <= 599:
            safe[key] = value
        elif key in {"input_tokens", "output_tokens", "total_tokens", "output_token_limit"}:
            safe[key] = value if type(value) is int and 0 <= value <= 10**9 else None
        elif key in {"provider_called", "reviewer_authenticated"} and type(value) is bool:
            safe[key] = value
    return safe


class LocalObserver:
    def __init__(self, *, capacity=1000):
        if type(capacity) is not int or not 1 <= capacity <= 100000:
            raise ValueError("capacity must be between 1 and 100000")
        self._events = deque(maxlen=capacity)
        self._metrics = {}
        self._evicted = 0
        self._logging_failures = 0
        self._lock = RLock()

    def record(self, event):
        # Called with application-produced closed-schema events only.
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._evicted += 1
            self._events.append(deepcopy(event))
            metric = self._metrics.setdefault(event["operation"], {"count": 0, "errors": 0,
                "duration_ms_total": 0.0, "duration_ms_max": 0.0, "provider_calls": 0,
                "input_tokens_known": 0, "output_tokens_known": 0, "total_tokens_known": 0,
                "input_tokens_unknown_calls": 0, "output_tokens_unknown_calls": 0, "total_tokens_unknown_calls": 0})
            metric["count"] += 1
            metric["errors"] += int(event["error"] is not None)
            metric["duration_ms_total"] += event["duration_ms"]
            metric["duration_ms_max"] = max(metric["duration_ms_max"], event["duration_ms"])
            if event.get("provider_called"):
                metric["provider_calls"] += 1
                for name in ("input_tokens", "output_tokens", "total_tokens"):
                    value = event.get(name)
                    metric[name + "_unknown_calls"] += int(value is None)
                    metric[name + "_known"] += value if value is not None else 0
        try:
            LOGGER.info(json.dumps(event, sort_keys=True, allow_nan=False))
        except Exception:
            with self._lock:
                self._logging_failures += 1

    def snapshot(self):
        """Local diagnostics follow the existing REVIEWER/ADMIN audit permission."""
        from .security import Role, require_roles
        require_roles(Role.REVIEWER, Role.ADMIN)
        with self._lock:
            return deepcopy({"events": list(self._events), "metrics": self._metrics,
                             "evicted_events": self._evicted, "logging_failures": self._logging_failures})


@contextmanager
def observing(observer):
    """Trusted-host binding; never accept the observer or trace context from model data."""
    token = _observer.set(observer)
    parent = _span.set(None)
    try:
        yield
    finally:
        _span.reset(parent)
        _observer.reset(token)


class Span:
    def __init__(self, operation):
        self.operation = operation
        parent = _span.get()
        self.trace_id = parent.trace_id if parent else str(uuid4())
        self.span_id = str(uuid4())
        self.parent_span_id = parent.span_id if parent else None
        self.fields = {}
        self.error = None

    def annotate(self, **fields):
        try:
            self.fields.update(_metadata(fields))
        except Exception:
            pass  # Diagnostics never interfere with application behavior.

    def fail(self, code):
        self.error = _error(code)


@contextmanager
def span(operation):
    if operation not in OPERATIONS:
        raise ValueError("Unknown telemetry operation")
    current = Span(operation)
    token = _span.set(current)
    started = perf_counter()
    try:
        yield current
    except Exception as error:
        current.fail(getattr(error, "code", None))
        raise
    finally:
        _span.reset(token)
        observer = _observer.get()
        if observer is not None:
            try:
                observer.record({"timestamp": datetime.now(timezone.utc).isoformat(), "operation": operation,
                    "trace_id": current.trace_id, "span_id": current.span_id, "parent_span_id": current.parent_span_id,
                    "duration_ms": max(0.0, (perf_counter() - started) * 1000), "error": current.error,
                    **current.fields})
            except Exception:
                pass  # No retry, rollback, or failure of a committed business operation.


def annotate(**fields):
    current = _span.get()
    if current is not None:
        current.annotate(**fields)


def observed(operation):
    """Synchronous execution boundaries; preserve returns and exception semantics."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(operation) as current:
                if operation == "tool":
                    current.annotate(tool=kwargs.get("name", args[1] if len(args) > 1 else None))
                result = function(*args, **kwargs)
                try:
                    final = getattr(result, "final_result", None) or result
                    failure = getattr(final, "error", None)
                    if failure:
                        current.fail(getattr(failure, "code", failure))
                    elif getattr(final, "ok", None) is False:
                        current.fail(getattr(final, "code", None))
                    current.annotate(status=getattr(final, "status", None))
                    if operation in {"hitl.start", "hitl.resume"}:
                        current.annotate(workflow_id=getattr(result, "workflow_id", None))
                    if operation in {"proposal.create", "proposal.review"}:
                        current.annotate(proposal_id=result.id, case_id=result.case_id)
                        if operation == "proposal.review":
                            from .security import Role, current_identity
                            current.annotate(reviewer_authenticated=False)
                            try:
                                identity = current_identity()
                                if identity.role in (Role.REVIEWER, Role.ADMIN):
                                    current.annotate(reviewer_user_id=identity.user_id, reviewer_authenticated=True)
                            except Exception:
                                pass  # Never use caller-supplied reviewer_name as identity.
                except Exception:
                    pass
                return result
        return wrapped
    return decorate
