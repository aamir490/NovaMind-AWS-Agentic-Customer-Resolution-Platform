"""Explicit local human-review operations; no execution endpoint."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from .proposals import HumanReview, ProposalCreate, ProposalService, ProposalStatus, ResolutionProposal
from .postgres_persistence import PostgreSQLHITLReviewService
from .routes import Service
from .security import Authorization, ReviewInput, Role, require_roles

router = APIRouter(prefix="/api", tags=["resolution-proposals"])


def get_proposals(request: Request) -> ProposalService:
    return request.app.state.proposal_service


def get_hitl_review_service(request: Request):
    return request.app.state.hitl_review_service


Proposals = Annotated[ProposalService, Depends(get_proposals)]
HITLReview = Annotated[PostgreSQLHITLReviewService | ProposalService, Depends(get_hitl_review_service)]


@router.post("/proposals", response_model=ResolutionProposal, status_code=201)
def create_proposal(body: ProposalCreate, proposals: Proposals, service: Service) -> ResolutionProposal:
    require_roles(Role.REVIEWER, Role.ADMIN)
    Authorization(service, proposals).case(body.case_id)
    return proposals.create(body)


@router.get("/proposals/{proposal_id}", response_model=ResolutionProposal)
def get_proposal(proposal_id: UUID, proposals: Proposals, service: Service) -> ResolutionProposal:
    Authorization(service, proposals).proposal(proposal_id)
    return proposals.get(proposal_id)


@router.get("/cases/{case_id}/proposals", response_model=list[ResolutionProposal])
def list_case_proposals(case_id: UUID, proposals: Proposals, service: Service) -> list[ResolutionProposal]:
    Authorization(service, proposals).case(case_id)
    return proposals.list_for_case(case_id)


@router.post("/proposals/{proposal_id}/approve", response_model=ResolutionProposal)
def approve_proposal(proposal_id: UUID, body: ReviewInput, proposals: Proposals,
                     hitl: HITLReview, service: Service) -> ResolutionProposal:
    identity = require_roles(Role.REVIEWER, Role.ADMIN)
    Authorization(service, proposals).proposal(proposal_id)
    return hitl.review(proposal_id, ProposalStatus.APPROVED,
                       HumanReview(reviewer_name=str(identity.user_id), note=body.note),
                       workflow_id=body.workflow_id, review_id=body.review_id)


@router.post("/proposals/{proposal_id}/reject", response_model=ResolutionProposal)
def reject_proposal(proposal_id: UUID, body: ReviewInput, proposals: Proposals,
                    hitl: HITLReview, service: Service) -> ResolutionProposal:
    identity = require_roles(Role.REVIEWER, Role.ADMIN)
    Authorization(service, proposals).proposal(proposal_id)
    return hitl.review(proposal_id, ProposalStatus.REJECTED,
                       HumanReview(reviewer_name=str(identity.user_id), note=body.note),
                       workflow_id=body.workflow_id, review_id=body.review_id)
