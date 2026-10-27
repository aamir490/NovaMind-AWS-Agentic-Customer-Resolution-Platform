"""Opt-in local Strands/Nova Lite smoke harness.

Default mode is completely offline: it uses synthetic records, read-only mock
tools, a mocked Strands agent, and a mocked provider.  Live Bedrock execution
requires both ``--live`` and the explicit ``novamind`` AWS profile.  This file
does not create an application, database connection, or durable job.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.domain import Order, OrderItem, SupportCase, CaseStatus
from backend.app.knowledge import (
    KnowledgeChunk, KnowledgeQuery, RetrievalHit, RetrievalResult, SourceMetadata,
)
from backend.app.resolution_drafts import (
    IssueType, RecommendedDecision, ResolutionDraft,
)
from backend.app.security import AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, authenticated
from backend.app.strands_agent import MAX_MODEL_CALLS, READ_ONLY_TOOLS, StrandsWorkflow
from backend.app.tools import ToolDescription, ToolError, ToolFailure, ToolSuccess
from backend.app.agent import AgentRequest


CASE_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_ID = UUID("22222222-2222-2222-2222-222222222222")
ORDER_ID = UUID("33333333-3333-3333-3333-333333333333")


class _SyntheticCases:
    def __init__(self, order):
        self._order = order

    def get_order(self, order_id):
        if UUID(str(order_id)) != self._order.id:
            raise KeyError("synthetic order not found")
        return self._order


class SyntheticReadOnlyTools:
    """Minimal tool facade with no storage or mutable operation bindings."""

    def __init__(self):
        self.order = Order(
            id=ORDER_ID, customer_id=CUSTOMER_ID,
            items=(OrderItem(sku="SMOKE-1", name="Synthetic device", quantity=1),),
        )
        self.case = SupportCase(
            id=CASE_ID, customer_id=CUSTOMER_ID, order_id=ORDER_ID,
            subject="Synthetic damaged item", description="The synthetic item arrived damaged.",
            status=CaseStatus.OPEN,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        self._cases = _SyntheticCases(self.order)
        self.invocations = []

    def describe(self):
        fields = {
            "search_knowledge": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "get_customer": {"customer_id": {"type": "string", "format": "uuid"}},
            "get_order": {"order_id": {"type": "string", "format": "uuid"}},
            "get_case": {"case_id": {"type": "string", "format": "uuid"}},
            "get_inventory": {"sku": {"type": "string"}},
            "get_policy": {"policy_id": {"type": "string"}},
            "assess_eligibility": {
                "customer_id": {"type": "string", "format": "uuid"},
                "sku": {"type": "string"}, "quantity": {"type": "integer"},
                "days_since_delivery": {"type": "integer"}, "reason": {"type": "string"},
                "condition": {"type": "string"}, "order_id": {"type": "string", "format": "uuid"},
                "case_id": {"type": "string", "format": "uuid"},
            },
            "get_proposal_status": {"proposal_id": {"type": "string", "format": "uuid"}},
        }
        return [ToolDescription(
            name=name,
            description="Synthetic read-only smoke-test lookup.",
            read_only=True,
            input_schema={
                "type": "object", "properties": fields.get(name, {}),
                "required": list(fields.get(name, {}).keys()),
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
        ) for name in sorted(READ_ONLY_TOOLS)]

    def invoke(self, name, arguments):
        self.invocations.append((name, dict(arguments)))
        if name == "get_case":
            return ToolSuccess(tool=name, data=self.case)
        if name == "get_order":
            return ToolSuccess(tool=name, data=self.order)
        return ToolFailure(tool=name, error=ToolError(
            code="NOT_FOUND", message="Synthetic smoke tool has no record for this lookup",
        ))

    def build_resolution_draft(self, case_id, message):
        return ResolutionDraft(
            issue_type=IssueType.DAMAGED,
            recommended_decision=RecommendedDecision.REQUEST_EVIDENCE,
            verified_facts=("synthetic_case",),
            missing_evidence=("customer-provided damage evidence",),
            decision_reason="Synthetic evidence requires staff review.",
            customer_facing_response="The synthetic investigation is informational only.",
        )

    def retrieve_grounded_policy(self, request):
        return _synthetic_retrieval(request)


def _synthetic_retrieval(request):
    text = "Synthetic reference guidance: collect evidence; no action is authorized."
    source = SourceMetadata(
        document_id="smoke-guide", title="Synthetic smoke guidance", version="v1",
        source_uri="local-knowledge://smoke-guide",
    )
    chunk = KnowledgeChunk(
        chunk_id=hashlib.sha256(text.encode()).hexdigest(), source=source, text=text,
        start_char=0, end_char=len(text),
    )
    return RetrievalResult(
        query=KnowledgeQuery.model_validate(request),
        hits=(RetrievalHit(chunk=chunk, score=1.0),),
    )


class _OfflineAgent:
    def __init__(self, **kwargs):
        self.model = kwargs["model"]

    def __call__(self, prompt):
        self.model.stream([], tool_specs=[])
        return types.SimpleNamespace(
            metrics=types.SimpleNamespace(tool_count=0),
            __str__=lambda self: "not-json",
        )


def _offline_model_factory():
    provider = Mock()
    provider.stream.return_value = Mock()
    return provider


def _live_model_factory(model_id, region):
    from strands.models import BedrockModel
    return BedrockModel(model_id=model_id, region_name=region)


def _reported_tool_calls(result, fallback):
    metrics = getattr(result, "metrics", None)
    tool_metrics = getattr(metrics, "tool_metrics", None)
    if isinstance(tool_metrics, dict):
        return sum(int(getattr(item, "call_count", 0) or 0) for item in tool_metrics.values())
    value = getattr(metrics, "tool_count", None)
    if value is not None:
        return int(value)
    bounded = getattr(result, "steps_used", None)
    return int(bounded) if bounded is not None else fallback


def run_smoke(*, live=False, profile="novamind", model_id="us.amazon.nova-lite-v1:0", region="us-east-1"):
    tools = SyntheticReadOnlyTools()
    identity = AuthenticatedIdentity(user_id=uuid4(), role="ADMIN")
    auth = LocalAuthenticationProvider((LocalCredential(
        token="strands-smoke-local-token", identity=identity,
    ),))

    if live:
        if profile != "novamind":
            raise ValueError("Live smoke tests require AWS profile novamind")
        from backend.container.bootstrap import _STRANDS_NOVA_LITE_MODEL_IDS
        if model_id not in _STRANDS_NOVA_LITE_MODEL_IDS:
            raise ValueError("Live smoke tests require an approved Nova Lite model ID")
        os.environ["AWS_PROFILE"] = "novamind"
        model_factory = lambda: _live_model_factory(model_id, region)
        workflow_factory = lambda: StrandsWorkflow(tools, model_factory=model_factory)
    else:
        fake_strands = types.ModuleType("strands")
        fake_strands.Agent = _OfflineAgent
        fake_strands.tool = lambda function, **kwargs: function
        with patch.dict(sys.modules, {"strands": fake_strands}):
            workflow_factory = lambda: StrandsWorkflow(tools, model_factory=_offline_model_factory)
            with authenticated(auth, "strands-smoke-local-token"):
                result = workflow_factory().start(AgentRequest(
                    case_id=CASE_ID, message="Please investigate this synthetic damaged item.",
                ))
        return {
            "mode": "offline", "status": result.status, "error": result.error,
            "model_call_limit": MAX_MODEL_CALLS,
            "tool_calls": _reported_tool_calls(result.agent_result, len(tools.invocations)),
            "aws_calls": 0, "actions_executed": False,
        }

    with authenticated(auth, "strands-smoke-local-token"):
        result = workflow_factory().start(AgentRequest(
            case_id=CASE_ID, message="Please investigate this synthetic damaged item.",
        ))
    return {
        "mode": "live", "status": result.status, "error": result.error,
        "model_call_limit": MAX_MODEL_CALLS,
        "tool_calls": _reported_tool_calls(result.agent_result, len(tools.invocations)),
        "aws_calls": "Bedrock attempted", "actions_executed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Opt in to one real Bedrock smoke run")
    parser.add_argument("--profile", default="novamind")
    parser.add_argument("--model-id", default="us.amazon.nova-lite-v1:0")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args(argv)
    if args.live and args.profile != "novamind":
        parser.error("--live requires --profile novamind")
    print(json.dumps(run_smoke(
        live=args.live, profile=args.profile, model_id=args.model_id, region=args.region,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
