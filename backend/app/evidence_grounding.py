"""Application-owned projection of retrieved policy guidance.

This deliberately keeps local knowledge separate from case records: a policy
passage is source-traceable reference material, while verified facts come only
from the deterministic resolution draft built from application records.
"""

import re
from typing import Literal

from pydantic import Field

from .domain import Record
from .knowledge import KnowledgeQuery, RetrievalResult
from .resolution_drafts import (
    IssueType, RecommendedDecision, RefundRecommendationOutcome, ResolutionCitation,
    ResolutionDraft, ResponseSource,
)
from .guardrails import GuardrailError, check_payload


class PolicyPassage(Record):
    """A bounded, citable policy excerpt copied from one retriever hit."""

    document_id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    source_uri: str = Field(pattern=r"^local-knowledge://[a-z0-9-]{1,80}$")
    passage: str = Field(min_length=1, max_length=600)
    authority: Literal["reference_only"] = "reference_only"
    trust: Literal["untrusted_information"] = "untrusted_information"


class EvidenceGrounding(Record):
    """Read-only RAG context supplied by the application, not the model."""

    issue_type: IssueType
    policy_passages: tuple[PolicyPassage, ...] = Field(min_length=1, max_length=5)
    verified_customer_facts: tuple[str, ...] = Field(min_length=1, max_length=8)
    missing_evidence: tuple[str, ...] = Field(min_length=1, max_length=6)
    product_skus: tuple[str, ...] = Field(default=(), max_length=8)
    policy_knowledge_authoritative: Literal[False] = False
    actions_executed: Literal[False] = False


class GroundedResponse(Record):
    """Optional model wording, bound to every application-owned grounding field."""

    issue_type: IssueType
    recommended_decision: RecommendedDecision
    verified_facts: tuple[str, ...] = Field(min_length=1, max_length=8)
    missing_evidence: tuple[str, ...] = Field(min_length=1, max_length=6)
    policy_passages: tuple[PolicyPassage, ...] = Field(min_length=1, max_length=5)
    decision_reason: str = Field(min_length=1, max_length=1200)
    customer_facing_response: str = Field(min_length=1, max_length=1200)
    troubleshooting_steps: tuple[str, ...] = Field(default=(), max_length=5)
    refund_outcome: RefundRecommendationOutcome | None = None
    refund_sku: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    actions_executed: Literal[False] = False


_POLICY_QUERIES: dict[IssueType, str] = {
    IssueType.DAMAGED: "damaged item warranty policy troubleshooting evidence",
    IssueType.WRONG_ITEM: "wrong item delivery fulfillment policy evidence",
    IssueType.MISSING_ITEM: "missing item delivery fulfillment policy evidence",
    IssueType.DELAYED_DELIVERY: "delayed delivery shipment tracking policy evidence",
    IssueType.MALFUNCTION: "malfunction warranty troubleshooting policy evidence",
    IssueType.RETURN: "return policy eligibility evidence",
    IssueType.REFUND_NOT_RECEIVED: "refund not received payment settlement policy evidence",
}


def policy_query_for(issue_type: IssueType, product_skus: tuple[str, ...] = ()) -> KnowledgeQuery:
    """Use a fixed server-side query; customer prose never controls retrieval."""
    skus = tuple(sorted({sku for sku in product_skus if isinstance(sku, str) and sku}))
    return KnowledgeQuery(query=" ".join((_POLICY_QUERIES[IssueType(issue_type)], *skus)), top_k=3)


def build_evidence_grounding(draft: ResolutionDraft, retrieval: RetrievalResult,
                             *, product_skus: tuple[str, ...] = ()) -> EvidenceGrounding:
    """Create citations only by copying validated retriever hits.

    An empty result is not a policy conclusion.  The caller must fail closed
    rather than asking an LLM to fill the citation or eligibility gap.
    """
    draft = ResolutionDraft.model_validate(draft)
    retrieval = RetrievalResult.model_validate(retrieval)
    if not retrieval.hits:
        raise ValueError("No policy passages were retrieved")
    passages = tuple(PolicyPassage(
        document_id=hit.chunk.source.document_id,
        chunk_id=hit.chunk.chunk_id,
        title=hit.chunk.source.title,
        version=hit.chunk.source.version,
        source_uri=hit.chunk.source.source_uri,
        passage=hit.chunk.text,
    ) for hit in retrieval.hits)
    if len({passage.chunk_id for passage in passages}) != len(passages):
        raise ValueError("Duplicate policy passages")
    return EvidenceGrounding(
        issue_type=draft.issue_type,
        policy_passages=passages,
        verified_customer_facts=draft.verified_facts,
        missing_evidence=draft.missing_evidence,
        product_skus=tuple(sorted(set(product_skus))),
    )


def attach_grounding_citations(draft: ResolutionDraft, grounding: EvidenceGrounding) -> ResolutionDraft:
    """Expose only retriever-owned citation metadata alongside a safe draft."""
    draft = ResolutionDraft.model_validate(draft)
    grounding = EvidenceGrounding.model_validate(grounding)
    citations = tuple(ResolutionCitation(
        document_id=passage.document_id,
        chunk_id=passage.chunk_id,
        title=passage.title,
        version=passage.version,
        source_uri=passage.source_uri,
    ) for passage in grounding.policy_passages)
    recommendation = draft.refund_recommendation
    if recommendation is not None:
        recommendation = recommendation.model_copy(update={"citations": citations})
    return draft.model_copy(update={"citations": citations, "refund_recommendation": recommendation})


def _approved_product_steps(grounding: EvidenceGrounding) -> tuple[str, ...]:
    """Extract only numbered steps from passages that name the case-bound SKU."""
    steps = []
    for passage in grounding.policy_passages:
        text = passage.passage
        if not any(re.search(rf"\b{re.escape(sku)}\b", text, re.IGNORECASE)
                   for sku in grounding.product_skus):
            continue
        for line in text.splitlines():
            match = re.match(r"\s*\d+\.\s+(.+?)\s*$", line)
            if match:
                steps.append(match.group(1))
    return tuple(dict.fromkeys(steps))


def apply_grounded_response(draft: ResolutionDraft, grounding: EvidenceGrounding,
                            candidate: GroundedResponse | None) -> ResolutionDraft | None:
    """Accept prose only when every authoritative field is an exact echo.

    The LLM has no merge authority: a failed validation returns ``None`` and
    callers retain the deterministic draft unchanged.
    """
    if candidate is None:
        return None
    try:
        draft = ResolutionDraft.model_validate(draft)
        grounding = EvidenceGrounding.model_validate(grounding)
        candidate = GroundedResponse.model_validate(candidate)
        if (candidate.issue_type != draft.issue_type
                or candidate.recommended_decision != draft.recommended_decision
                or candidate.verified_facts != draft.verified_facts
                or candidate.missing_evidence != draft.missing_evidence
                or candidate.policy_passages != grounding.policy_passages):
            return None
        recommendation = draft.refund_recommendation
        if recommendation is None:
            if candidate.refund_outcome is not None or candidate.refund_sku is not None:
                return None
        elif (candidate.refund_outcome != recommendation.outcome
              or candidate.refund_sku != recommendation.sku):
            return None
        approved_steps = _approved_product_steps(grounding)
        if candidate.troubleshooting_steps:
            if not approved_steps or any(step not in approved_steps for step in candidate.troubleshooting_steps):
                return None
            numbered = tuple(f"{index}. {step}" for index, step in enumerate(candidate.troubleshooting_steps, 1))
            if any(step not in candidate.customer_facing_response for step in numbered):
                return None
        elif draft.issue_type is IssueType.MALFUNCTION and approved_steps:
            # Product-specific instructions exist, but the model did not bind
            # its customer wording to them. Keep the deterministic fallback.
            return None
        check_payload(candidate.model_dump(mode="json"))
        wording = (candidate.decision_reason + " " + candidate.customer_facing_response).casefold()
        if "no refund, replacement, return, or payment action has been executed" not in wording:
            return None
        if candidate.recommended_decision is RecommendedDecision.REQUEST_EVIDENCE and "evidence" not in wording:
            return None
        if candidate.recommended_decision is RecommendedDecision.ESCALATE and not re.search(r"\b(staff|review)\b", wording):
            return None
        if re.search(r"\b(approved|approval|eligible|refund (?:has been|was) (?:issued|sent|processed))\b", wording):
            return None
        return draft.model_copy(update={
            "decision_reason": candidate.decision_reason,
            "customer_facing_response": candidate.customer_facing_response,
            "troubleshooting_steps": candidate.troubleshooting_steps,
            "response_source": ResponseSource.BEDROCK_VALIDATED,
            "citations": tuple(ResolutionCitation(
                document_id=passage.document_id,
                chunk_id=passage.chunk_id,
                title=passage.title,
                version=passage.version,
                source_uri=passage.source_uri,
            ) for passage in grounding.policy_passages),
        })
    except (ValueError, GuardrailError):
        return None
