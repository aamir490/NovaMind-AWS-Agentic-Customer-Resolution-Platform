"""M5B SQLite schema and transaction foundation; not wired into application services."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any, ContextManager, Iterator, Literal, Protocol
from uuid import UUID, uuid4

from .domain import (
    CaseCustomerCommunication, CaseResolutionDecision, CaseStatus, Customer, Order, OrderItem,
    InvestigationExecutionIdentity, InvestigationJob, InvestigationJobClaim,
    InvestigationExecutionAttempt, InvestigationExecutionContext, InvestigationJobStatus,
    InvestigationRecoveryReason, InvestigationControl, InvestigationMode,
    InvestigationModeAuditEvent,
    ResolutionAction, SupportCase, can_transition,
)
from .agent import AgentRequest, agent_request_fingerprint
from .llm import InvalidResponseCategory, InvalidResponseStage
from .operations import (
    AuthoritativeDeliveryFact, EligibilityEvidence, EligibilityResult,
    TrustedDeliverySource,
)
from .proposals import (
    HumanReview, ProposalStatus, ResolutionProposal,
    WorkflowReviewBinding,
)
from .service import ConflictError, NotFoundError
from .security import CognitoCustomerBinding, trusted_investigation_execution
from .workflow_metadata import (
    AGENT_FAILURE_CATEGORIES, FailureCategory, FailureStage,
    WORKFLOW_STATUS_TRANSITIONS, WorkflowMetadataStatus, WorkflowReservationResult,
    WorkflowRunEvent, WorkflowRunMetadata, WorkflowRunReservation,
)


class PersistenceError(Exception):
    """Storage failure safe to expose only as a generic application code."""


_POSTGRES_HOUR_OFFSET_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)([+-]\d{2})$"
)


def _canonical_legacy_postgres_timestamp(value: Any) -> Any:
    """Normalize only PostgreSQL's legacy hour-only UTC-offset text format.

    Migration 4 inserted PostgreSQL's ``CURRENT_TIMESTAMP::text`` output.
    PostgreSQL accepts an hour-only offset (``+00``), while the strict domain
    reader accepts canonical offsets (``+00:00``).  All other inputs still go
    through the existing Pydantic validation unchanged.
    """

    if isinstance(value, str):
        match = _POSTGRES_HOUR_OFFSET_TIMESTAMP.fullmatch(value)
        if match is not None:
            return f"{match.group(1)}{match.group(2)}:00"
    return value


class UnitOfWork(Protocol):
    """The repositories require a DB-API-like transactional connection only."""

    def transaction(self) -> ContextManager[Any]: ...


class BusinessRepository(Protocol):
    """Future customer/order/case repository boundary."""

    def transaction(self) -> ContextManager[Any]: ...


class WorkflowRepository(Protocol):
    """Future proposal, review, audit, and workflow metadata boundary."""

    def transaction(self) -> ContextManager[Any]: ...


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]
    requires_foreign_keys_off: bool = False


MIGRATIONS = (
    Migration(1, (
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY NOT NULL,
            applied_at TEXT NOT NULL
        )""",
        """CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 120)
        )""",
        """CREATE TABLE orders (
            order_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX orders_customer ON orders(customer_id, created_at, order_id)",
        """CREATE TABLE order_items (
            order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            sku TEXT NOT NULL CHECK(length(trim(sku)) BETWEEN 1 AND 120),
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 120),
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000),
            PRIMARY KEY(order_id, sku)
        )""",
        """CREATE TABLE support_cases (
            case_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            order_id TEXT NOT NULL REFERENCES orders(order_id),
            subject TEXT NOT NULL CHECK(length(trim(subject)) BETWEEN 1 AND 200),
            description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 4000),
            status TEXT NOT NULL CHECK(status IN ('open', 'in_review', 'escalated')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX support_cases_customer ON support_cases(customer_id, updated_at, case_id)",
        "CREATE INDEX support_cases_order ON support_cases(order_id, case_id)",
        """CREATE TABLE trusted_delivery_sources (
            source_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(source_id)) BETWEEN 1 AND 120)
        )""",
        """CREATE TABLE delivery_facts (
            delivery_fact_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            order_id TEXT NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            sku TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000),
            delivered_at TEXT NOT NULL,
            reason TEXT NOT NULL CHECK(reason IN ('wrong_item', 'damaged', 'change_of_mind')),
            item_condition TEXT NOT NULL CHECK(item_condition IN ('unused', 'used', 'damaged')),
            source_id TEXT NOT NULL REFERENCES trusted_delivery_sources(source_id),
            source_reference TEXT NOT NULL CHECK(length(trim(source_reference)) BETWEEN 1 AND 120),
            verified_at TEXT NOT NULL,
            FOREIGN KEY(order_id, sku) REFERENCES order_items(order_id, sku),
            UNIQUE(case_id, delivery_fact_id),
            UNIQUE(case_id, sku),
            UNIQUE(source_id, source_reference)
        )""",
        """CREATE TABLE eligibility_evidence (
            evidence_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            order_id TEXT NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            sku TEXT NOT NULL,
            delivery_fact_id TEXT NOT NULL REFERENCES delivery_facts(delivery_fact_id),
            policy_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            assessment_json TEXT NOT NULL CHECK(json_valid(assessment_json)),
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id, sku) REFERENCES order_items(order_id, sku),
            FOREIGN KEY(case_id, delivery_fact_id) REFERENCES delivery_facts(case_id, delivery_fact_id),
            UNIQUE(case_id, evidence_id)
        )""",
        "CREATE INDEX eligibility_evidence_case ON eligibility_evidence(case_id, created_at, evidence_id)",
        """CREATE TABLE resolution_proposals (
            proposal_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            action TEXT NOT NULL CHECK(action IN ('refund', 'replacement', 'return')),
            rationale TEXT NOT NULL CHECK(length(trim(rationale)) BETWEEN 1 AND 4000),
            eligibility_evidence_id TEXT REFERENCES eligibility_evidence(evidence_id),
            status TEXT NOT NULL CHECK(status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_user_id TEXT,
            review_note TEXT,
            CHECK((action = 'refund' AND eligibility_evidence_id IS NOT NULL)
                  OR (action <> 'refund' AND eligibility_evidence_id IS NULL)),
            CHECK((status = 'PENDING_REVIEW' AND reviewed_at IS NULL AND reviewer_user_id IS NULL AND review_note IS NULL)
                  OR (status IN ('APPROVED', 'REJECTED') AND reviewed_at IS NOT NULL
                      AND reviewer_user_id IS NOT NULL AND review_note IS NOT NULL)),
            FOREIGN KEY(case_id, eligibility_evidence_id) REFERENCES eligibility_evidence(case_id, evidence_id),
            UNIQUE(case_id, proposal_id)
        )""",
        "CREATE INDEX resolution_proposals_case ON resolution_proposals(case_id, status, created_at, proposal_id)",
        """CREATE TABLE workflow_runs (
            workflow_id TEXT PRIMARY KEY NOT NULL,
            run_id TEXT UNIQUE NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            owner_user_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            provider TEXT NOT NULL CHECK(provider IN ('local_scripted', 'bedrock')),
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(owner_user_id, request_id),
            UNIQUE(workflow_id, case_id)
        )""",
        "CREATE INDEX workflow_runs_case ON workflow_runs(case_id, updated_at, workflow_id)",
        """CREATE TABLE workflow_review_bindings (
            proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES resolution_proposals(proposal_id),
            workflow_id TEXT UNIQUE NOT NULL REFERENCES workflow_runs(workflow_id),
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            review_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id, proposal_id) REFERENCES resolution_proposals(case_id, proposal_id),
            FOREIGN KEY(workflow_id, case_id) REFERENCES workflow_runs(workflow_id, case_id)
        )""",
        """CREATE TABLE workflow_audit_events (
            workflow_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            kind TEXT NOT NULL CHECK(kind IN ('AGENT', 'REVIEW_REQUIRED', 'HUMAN_REVIEW', 'FAILURE')),
            proposal_id TEXT REFERENCES resolution_proposals(proposal_id),
            reviewer_user_id TEXT,
            proposal_status TEXT CHECK(proposal_status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            event_json TEXT NOT NULL CHECK(json_valid(event_json)),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workflow_id, sequence)
        )""",
    )),
    Migration(2, (
        """CREATE TRIGGER immutable_trusted_delivery_sources_update
           BEFORE UPDATE ON trusted_delivery_sources BEGIN
             SELECT RAISE(ABORT, 'trusted delivery sources are immutable');
           END""",
        """CREATE TRIGGER immutable_trusted_delivery_sources_delete
           BEFORE DELETE ON trusted_delivery_sources BEGIN
             SELECT RAISE(ABORT, 'trusted delivery sources are immutable');
           END""",
        """CREATE TRIGGER immutable_delivery_facts_update
           BEFORE UPDATE ON delivery_facts BEGIN
             SELECT RAISE(ABORT, 'delivery facts are immutable');
           END""",
        """CREATE TRIGGER immutable_delivery_facts_delete
           BEFORE DELETE ON delivery_facts BEGIN
             SELECT RAISE(ABORT, 'delivery facts are immutable');
           END""",
        """CREATE TRIGGER immutable_eligibility_evidence_update
           BEFORE UPDATE ON eligibility_evidence BEGIN
             SELECT RAISE(ABORT, 'eligibility evidence is immutable');
           END""",
        """CREATE TRIGGER immutable_eligibility_evidence_delete
           BEFORE DELETE ON eligibility_evidence BEGIN
             SELECT RAISE(ABORT, 'eligibility evidence is immutable');
           END""",
        """CREATE TRIGGER validate_delivery_fact_bindings
           BEFORE INSERT ON delivery_facts BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM support_cases c
               JOIN orders o ON o.order_id = c.order_id
               JOIN order_items i ON i.order_id = o.order_id
               WHERE c.case_id = NEW.case_id
                 AND c.order_id = NEW.order_id
                 AND c.customer_id = NEW.customer_id
                 AND o.customer_id = NEW.customer_id
                 AND i.sku = NEW.sku
                 AND NEW.quantity <= i.quantity
             ) THEN RAISE(ABORT, 'invalid delivery fact bindings') END;
           END""",
        """CREATE TRIGGER validate_eligibility_evidence_bindings
           BEFORE INSERT ON eligibility_evidence BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM delivery_facts f
               WHERE f.delivery_fact_id = NEW.delivery_fact_id
                 AND f.case_id = NEW.case_id
                 AND f.order_id = NEW.order_id
                 AND f.customer_id = NEW.customer_id
                 AND f.sku = NEW.sku
             ) THEN RAISE(ABORT, 'invalid eligibility evidence bindings') END;
             SELECT CASE WHEN
               json_extract(NEW.assessment_json, '$.evidence_id') IS NOT NEW.evidence_id OR
               json_extract(NEW.assessment_json, '$.case_id') IS NOT NEW.case_id OR
               json_extract(NEW.assessment_json, '$.order_id') IS NOT NEW.order_id OR
               json_extract(NEW.assessment_json, '$.customer_id') IS NOT NEW.customer_id OR
               json_extract(NEW.assessment_json, '$.sku') IS NOT NEW.sku OR
               json_extract(NEW.assessment_json, '$.policy_id') IS NOT NEW.policy_id OR
               json_extract(NEW.assessment_json, '$.policy_version') IS NOT NEW.policy_version OR
               json_extract(NEW.assessment_json, '$.assessment_basis') IS NOT 'trusted_delivery_record' OR
               json_extract(NEW.assessment_json, '$.authorization_granted') IS NOT 0 OR
               json_extract(NEW.assessment_json, '$.return_eligible') IS NOT
                 json_extract(NEW.assessment_json, '$.refund_eligible') OR
               json_extract(NEW.assessment_json, '$.refund_eligible') IS NOT
                 (json_array_length(json_extract(NEW.assessment_json, '$.denial_reasons')) = 0) OR
               NOT EXISTS (
                 SELECT 1 FROM delivery_facts f
                 JOIN order_items i ON i.order_id = f.order_id AND i.sku = f.sku
                 WHERE f.delivery_fact_id = NEW.delivery_fact_id
                   AND json_extract(NEW.assessment_json, '$.requested_quantity') = f.quantity
                   AND json_extract(NEW.assessment_json, '$.purchased_quantity') = i.quantity
                   AND json_extract(NEW.assessment_json, '$.reason') = f.reason
                   AND json_extract(NEW.assessment_json, '$.condition') = f.item_condition
               )
             THEN RAISE(ABORT, 'invalid eligibility assessment snapshot') END;
           END""",
    )),
    Migration(3, (
        """CREATE TRIGGER validate_resolution_proposal_insert
           BEFORE INSERT ON resolution_proposals BEGIN
             SELECT CASE WHEN NEW.status IS NOT 'PENDING_REVIEW'
                 OR NEW.updated_at IS NOT NEW.created_at
                 OR NEW.reviewed_at IS NOT NULL
                 OR NEW.reviewer_user_id IS NOT NULL
                 OR NEW.review_note IS NOT NULL
               THEN RAISE(ABORT, 'invalid new proposal state') END;
             SELECT CASE WHEN NEW.action = 'refund' AND NOT EXISTS (
               SELECT 1 FROM eligibility_evidence e
               WHERE e.evidence_id = NEW.eligibility_evidence_id
                 AND e.case_id = NEW.case_id
                 AND json_extract(e.assessment_json, '$.refund_eligible') = 1
                 AND json_array_length(json_extract(e.assessment_json, '$.denial_reasons')) = 0
             ) THEN RAISE(ABORT, 'refund proposal requires eligible evidence') END;
           END""",
        """CREATE TRIGGER validate_resolution_proposal_update
           BEFORE UPDATE ON resolution_proposals BEGIN
             SELECT CASE WHEN
               OLD.proposal_id IS NOT NEW.proposal_id OR
               OLD.case_id IS NOT NEW.case_id OR
               OLD.action IS NOT NEW.action OR
               OLD.rationale IS NOT NEW.rationale OR
               OLD.eligibility_evidence_id IS NOT NEW.eligibility_evidence_id OR
               OLD.created_at IS NOT NEW.created_at OR
               OLD.status IS NOT 'PENDING_REVIEW' OR
               NEW.status NOT IN ('APPROVED', 'REJECTED') OR
               NEW.updated_at <= OLD.updated_at OR
               NEW.reviewed_at IS NOT NEW.updated_at OR
               NEW.reviewer_user_id IS NULL OR
               length(trim(NEW.reviewer_user_id)) NOT BETWEEN 1 AND 120 OR
               NEW.review_note IS NULL OR
               length(trim(NEW.review_note)) NOT BETWEEN 1 AND 4000
             THEN RAISE(ABORT, 'invalid proposal review transition') END;
           END""",
        """CREATE TRIGGER immutable_resolution_proposals_delete
           BEFORE DELETE ON resolution_proposals BEGIN
             SELECT RAISE(ABORT, 'resolution proposals are immutable');
           END""",
        """CREATE TRIGGER validate_workflow_review_binding_insert
           BEFORE INSERT ON workflow_review_bindings BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM resolution_proposals p
               JOIN workflow_runs w ON w.workflow_id = NEW.workflow_id
               WHERE p.proposal_id = NEW.proposal_id
                 AND p.case_id = NEW.case_id
                 AND p.status = 'PENDING_REVIEW'
                 AND w.case_id = NEW.case_id
                 AND w.status = 'REVIEW_REQUIRED'
             ) THEN RAISE(ABORT, 'invalid workflow review binding') END;
           END""",
        """CREATE TRIGGER immutable_workflow_review_bindings_update
           BEFORE UPDATE ON workflow_review_bindings BEGIN
             SELECT RAISE(ABORT, 'workflow review bindings are immutable');
           END""",
        """CREATE TRIGGER immutable_workflow_review_bindings_delete
           BEFORE DELETE ON workflow_review_bindings BEGIN
             SELECT RAISE(ABORT, 'workflow review bindings are immutable');
           END""",
        """CREATE TRIGGER validate_workflow_audit_event_insert
           BEFORE INSERT ON workflow_audit_events BEGIN
             SELECT CASE WHEN
               json_extract(NEW.event_json, '$.kind') IS NOT NEW.kind OR
               json_extract(NEW.event_json, '$.workflow_id') IS NOT NEW.workflow_id OR
               json_extract(NEW.event_json, '$.proposal_id') IS NOT NEW.proposal_id OR
               json_extract(NEW.event_json, '$.proposal_status') IS NOT NEW.proposal_status OR
               json_extract(NEW.event_json, '$.reviewer_user_id') IS NOT NEW.reviewer_user_id OR
               json_extract(NEW.event_json, '$.review_note') IS NOT (
                 SELECT review_note FROM resolution_proposals
                 WHERE proposal_id = NEW.proposal_id
               ) OR
               json_extract(NEW.event_json, '$.case_id') IS NOT (
                 SELECT case_id FROM resolution_proposals
                 WHERE proposal_id = NEW.proposal_id
               ) OR
               NOT EXISTS (
                 SELECT 1 FROM workflow_runs w
                 JOIN resolution_proposals p ON p.proposal_id = NEW.proposal_id
                 WHERE w.workflow_id = NEW.workflow_id
                   AND w.case_id = p.case_id
               )
             THEN RAISE(ABORT, 'invalid workflow audit payload') END;
             SELECT CASE WHEN NEW.kind = 'REVIEW_REQUIRED' AND (
               NEW.proposal_id IS NULL OR
               NEW.reviewer_user_id IS NOT NULL OR
               NEW.proposal_status IS NOT 'PENDING_REVIEW' OR
               NOT EXISTS (
                 SELECT 1 FROM workflow_review_bindings b
                 WHERE b.workflow_id = NEW.workflow_id
                   AND b.proposal_id = NEW.proposal_id
                   AND b.review_id = json_extract(NEW.event_json, '$.review_id')
               )
             ) THEN RAISE(ABORT, 'invalid review-required audit event') END;
             SELECT CASE WHEN NEW.kind = 'HUMAN_REVIEW' AND (
               NEW.proposal_id IS NULL OR
               NEW.reviewer_user_id IS NULL OR
               NEW.proposal_status NOT IN ('APPROVED', 'REJECTED') OR
               NOT EXISTS (
                 SELECT 1 FROM resolution_proposals p
                 WHERE p.proposal_id = NEW.proposal_id
                   AND p.status = NEW.proposal_status
                   AND p.reviewer_user_id = NEW.reviewer_user_id
               ) OR NOT (
                 (EXISTS (
                   SELECT 1 FROM workflow_review_bindings b
                   WHERE b.workflow_id = NEW.workflow_id
                     AND b.proposal_id = NEW.proposal_id
                     AND b.review_id = json_extract(NEW.event_json, '$.review_id')
                 )) OR (
                   NOT EXISTS (
                     SELECT 1 FROM workflow_review_bindings b
                     WHERE b.proposal_id = NEW.proposal_id
                   ) AND json_extract(NEW.event_json, '$.review_id') IS NULL
                 )
               )
             ) THEN RAISE(ABORT, 'invalid human-review audit event') END;
           END""",
        """CREATE TRIGGER immutable_workflow_audit_events_update
           BEFORE UPDATE ON workflow_audit_events BEGIN
             SELECT RAISE(ABORT, 'workflow audit events are immutable');
           END""",
        """CREATE TRIGGER immutable_workflow_audit_events_delete
           BEFORE DELETE ON workflow_audit_events BEGIN
             SELECT RAISE(ABORT, 'workflow audit events are immutable');
           END""",
    )),
    Migration(4, (
        """CREATE TABLE workflow_run_events (
            workflow_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            kind TEXT NOT NULL CHECK(kind IN ('CREATED', 'STATUS_CHANGED', 'RECOVERY_REQUIRED')),
            previous_status TEXT CHECK(previous_status IN
                ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            status TEXT NOT NULL CHECK(status IN
                ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            event_json TEXT NOT NULL CHECK(json_valid(event_json)),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workflow_id, sequence)
        )""",
        """CREATE TRIGGER validate_workflow_run_insert
           BEFORE INSERT ON workflow_runs
           WHEN NEW.owner_user_id <> 'proposal-review-persistence'
           BEGIN
             SELECT CASE WHEN NEW.status IS NOT 'RUNNING'
                 OR NEW.updated_at IS NOT NEW.created_at
                 OR length(NEW.request_fingerprint) <> 64
                 OR lower(NEW.request_fingerprint) GLOB '*[^0-9a-f]*'
               THEN RAISE(ABORT, 'invalid workflow run reservation') END;
           END""",
        """CREATE TRIGGER validate_workflow_run_update
           BEFORE UPDATE ON workflow_runs BEGIN
             SELECT CASE WHEN
               OLD.workflow_id IS NOT NEW.workflow_id OR
               OLD.run_id IS NOT NEW.run_id OR
               OLD.case_id IS NOT NEW.case_id OR
               OLD.owner_user_id IS NOT NEW.owner_user_id OR
               OLD.request_id IS NOT NEW.request_id OR
               OLD.request_fingerprint IS NOT NEW.request_fingerprint OR
               OLD.provider IS NOT NEW.provider OR
               OLD.created_at IS NOT NEW.created_at OR
               NEW.updated_at <= OLD.updated_at OR
               NOT (
                 (OLD.status = 'RUNNING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN
                   ('RESUMING', 'REVIEWED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'RESUMING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'REVIEWED', 'FAILED', 'RECOVERY_REQUIRED'))
               )
             THEN RAISE(ABORT, 'invalid workflow status transition') END;
           END""",
        """CREATE TRIGGER immutable_workflow_runs_delete
           BEFORE DELETE ON workflow_runs BEGIN
             SELECT RAISE(ABORT, 'workflow runs are immutable');
           END""",
        """CREATE TRIGGER validate_workflow_run_event_insert
           BEFORE INSERT ON workflow_run_events BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM workflow_runs w
               WHERE w.workflow_id = NEW.workflow_id
                 AND w.status = NEW.status
             ) OR
               json_extract(NEW.event_json, '$.workflow_id') IS NOT NEW.workflow_id OR
               json_extract(NEW.event_json, '$.sequence') IS NOT NEW.sequence OR
               json_extract(NEW.event_json, '$.kind') IS NOT NEW.kind OR
               json_extract(NEW.event_json, '$.previous_status') IS NOT NEW.previous_status OR
               json_extract(NEW.event_json, '$.status') IS NOT NEW.status OR
               json_extract(NEW.event_json, '$.case_id') IS NOT (
                 SELECT case_id FROM workflow_runs WHERE workflow_id = NEW.workflow_id
               ) OR
               json_extract(NEW.event_json, '$.owner_user_id') IS NOT (
                 SELECT owner_user_id FROM workflow_runs WHERE workflow_id = NEW.workflow_id
               )
             THEN RAISE(ABORT, 'invalid workflow run event') END;
             SELECT CASE WHEN
               (NEW.kind = 'CREATED' AND (NEW.sequence <> 1 OR NEW.previous_status IS NOT NULL
                                          OR NEW.status <> 'RUNNING')) OR
               (NEW.kind = 'RECOVERY_REQUIRED' AND NEW.status <> 'RECOVERY_REQUIRED') OR
               (NEW.kind = 'STATUS_CHANGED' AND NEW.previous_status IS NULL)
             THEN RAISE(ABORT, 'invalid workflow run event transition') END;
           END""",
        """CREATE TRIGGER immutable_workflow_run_events_update
           BEFORE UPDATE ON workflow_run_events BEGIN
             SELECT RAISE(ABORT, 'workflow run events are immutable');
           END""",
        """CREATE TRIGGER immutable_workflow_run_events_delete
           BEFORE DELETE ON workflow_run_events BEGIN
             SELECT RAISE(ABORT, 'workflow run events are immutable');
           END""",
    )),
    Migration(5, (
        """CREATE TABLE langgraph_checkpoints (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
            checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            checkpoint_type TEXT NOT NULL,
            checkpoint_blob BLOB NOT NULL,
            checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash) = 64),
            metadata_type TEXT NOT NULL,
            metadata_blob BLOB NOT NULL,
            metadata_hash TEXT NOT NULL CHECK(length(metadata_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id),
            FOREIGN KEY(thread_id, checkpoint_ns, parent_checkpoint_id)
                REFERENCES langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id)
        )""",
        """CREATE INDEX langgraph_checkpoints_latest
           ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC)""",
        """CREATE TABLE langgraph_checkpoint_blobs (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
            checkpoint_ns TEXT NOT NULL,
            channel TEXT NOT NULL,
            version_key TEXT NOT NULL,
            value_type TEXT NOT NULL,
            value_blob BLOB NOT NULL,
            value_hash TEXT NOT NULL CHECK(length(value_hash) = 64),
            PRIMARY KEY(thread_id, checkpoint_ns, channel, version_key)
        )""",
        """CREATE TABLE langgraph_checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            write_index INTEGER NOT NULL,
            channel TEXT NOT NULL,
            value_type TEXT NOT NULL,
            value_blob BLOB NOT NULL,
            value_hash TEXT NOT NULL CHECK(length(value_hash) = 64),
            task_path TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index),
            FOREIGN KEY(thread_id, checkpoint_ns, checkpoint_id)
                REFERENCES langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id)
                ON DELETE CASCADE
        )""",
        """CREATE TRIGGER immutable_langgraph_checkpoints_update
           BEFORE UPDATE ON langgraph_checkpoints BEGIN
             SELECT RAISE(ABORT, 'langgraph checkpoints are immutable');
           END""",
        """CREATE TRIGGER immutable_langgraph_checkpoint_blobs_update
           BEFORE UPDATE ON langgraph_checkpoint_blobs BEGIN
             SELECT RAISE(ABORT, 'langgraph checkpoint blobs are immutable');
           END""",
        """CREATE TRIGGER immutable_langgraph_checkpoint_writes_update
           BEFORE UPDATE ON langgraph_checkpoint_writes BEGIN
             SELECT RAISE(ABORT, 'langgraph checkpoint writes are immutable');
           END""",
    )),
    Migration(6, (
        "PRAGMA defer_foreign_keys=ON",
        """CREATE TABLE resolution_proposals_v6 (
            proposal_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            action TEXT NOT NULL CHECK(action IN
                ('refund', 'refund_investigation', 'replacement', 'return')),
            rationale TEXT NOT NULL CHECK(length(trim(rationale)) BETWEEN 1 AND 4000),
            eligibility_evidence_id TEXT REFERENCES eligibility_evidence(evidence_id),
            status TEXT NOT NULL CHECK(status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_user_id TEXT,
            review_note TEXT,
            CHECK((action = 'refund' AND eligibility_evidence_id IS NOT NULL)
                  OR (action <> 'refund' AND eligibility_evidence_id IS NULL)),
            CHECK((status = 'PENDING_REVIEW' AND reviewed_at IS NULL
                   AND reviewer_user_id IS NULL AND review_note IS NULL)
                  OR (status IN ('APPROVED', 'REJECTED') AND reviewed_at IS NOT NULL
                      AND reviewer_user_id IS NOT NULL AND review_note IS NOT NULL)),
            FOREIGN KEY(case_id, eligibility_evidence_id)
                REFERENCES eligibility_evidence(case_id, evidence_id),
            UNIQUE(case_id, proposal_id)
        )""",
        """INSERT INTO resolution_proposals_v6
           SELECT * FROM resolution_proposals""",
        "DROP TRIGGER validate_workflow_review_binding_insert",
        "DROP TRIGGER validate_workflow_audit_event_insert",
        "DROP TABLE resolution_proposals",
        "ALTER TABLE resolution_proposals_v6 RENAME TO resolution_proposals",
        """CREATE INDEX resolution_proposals_case
           ON resolution_proposals(case_id, status, created_at, proposal_id)""",
        """CREATE TRIGGER validate_resolution_proposal_insert
           BEFORE INSERT ON resolution_proposals BEGIN
             SELECT CASE WHEN NEW.status IS NOT 'PENDING_REVIEW'
                 OR NEW.updated_at IS NOT NEW.created_at
                 OR NEW.reviewed_at IS NOT NULL
                 OR NEW.reviewer_user_id IS NOT NULL
                 OR NEW.review_note IS NOT NULL
               THEN RAISE(ABORT, 'invalid new proposal state') END;
             SELECT CASE WHEN NEW.action = 'refund' AND NOT EXISTS (
               SELECT 1 FROM eligibility_evidence e
               WHERE e.evidence_id = NEW.eligibility_evidence_id
                 AND e.case_id = NEW.case_id
                 AND json_extract(e.assessment_json, '$.refund_eligible') = 1
                 AND json_array_length(json_extract(e.assessment_json, '$.denial_reasons')) = 0
             ) THEN RAISE(ABORT, 'refund proposal requires eligible evidence') END;
           END""",
        """CREATE TRIGGER validate_resolution_proposal_update
           BEFORE UPDATE ON resolution_proposals BEGIN
             SELECT CASE WHEN
               OLD.proposal_id IS NOT NEW.proposal_id OR
               OLD.case_id IS NOT NEW.case_id OR
               OLD.action IS NOT NEW.action OR
               OLD.rationale IS NOT NEW.rationale OR
               OLD.eligibility_evidence_id IS NOT NEW.eligibility_evidence_id OR
               OLD.created_at IS NOT NEW.created_at OR
               OLD.status IS NOT 'PENDING_REVIEW' OR
               NEW.status NOT IN ('APPROVED', 'REJECTED') OR
               NEW.updated_at <= OLD.updated_at OR
               NEW.reviewed_at IS NOT NEW.updated_at OR
               NEW.reviewer_user_id IS NULL OR
               length(trim(NEW.reviewer_user_id)) NOT BETWEEN 1 AND 120 OR
               NEW.review_note IS NULL OR
               length(trim(NEW.review_note)) NOT BETWEEN 1 AND 4000
             THEN RAISE(ABORT, 'invalid proposal review transition') END;
           END""",
        """CREATE TRIGGER immutable_resolution_proposals_delete
           BEFORE DELETE ON resolution_proposals BEGIN
             SELECT RAISE(ABORT, 'resolution proposals are immutable');
           END""",
        """CREATE TRIGGER validate_workflow_review_binding_insert
           BEFORE INSERT ON workflow_review_bindings BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM resolution_proposals p
               JOIN workflow_runs w ON w.workflow_id = NEW.workflow_id
               WHERE p.proposal_id = NEW.proposal_id
                 AND p.case_id = NEW.case_id
                 AND p.status = 'PENDING_REVIEW'
                 AND w.case_id = NEW.case_id
                 AND w.status = 'REVIEW_REQUIRED'
             ) THEN RAISE(ABORT, 'invalid workflow review binding') END;
           END""",
        """CREATE TRIGGER validate_workflow_audit_event_insert
           BEFORE INSERT ON workflow_audit_events BEGIN
             SELECT CASE WHEN
               json_extract(NEW.event_json, '$.kind') IS NOT NEW.kind OR
               json_extract(NEW.event_json, '$.workflow_id') IS NOT NEW.workflow_id OR
               json_extract(NEW.event_json, '$.proposal_id') IS NOT NEW.proposal_id OR
               json_extract(NEW.event_json, '$.proposal_status') IS NOT NEW.proposal_status OR
               json_extract(NEW.event_json, '$.reviewer_user_id') IS NOT NEW.reviewer_user_id OR
               json_extract(NEW.event_json, '$.review_note') IS NOT (
                 SELECT review_note FROM resolution_proposals
                 WHERE proposal_id = NEW.proposal_id
               ) OR
               json_extract(NEW.event_json, '$.case_id') IS NOT (
                 SELECT case_id FROM resolution_proposals
                 WHERE proposal_id = NEW.proposal_id
               ) OR
               NOT EXISTS (
                 SELECT 1 FROM workflow_runs w
                 JOIN resolution_proposals p ON p.proposal_id = NEW.proposal_id
                 WHERE w.workflow_id = NEW.workflow_id
                   AND w.case_id = p.case_id
               )
             THEN RAISE(ABORT, 'invalid workflow audit payload') END;
             SELECT CASE WHEN NEW.kind = 'REVIEW_REQUIRED' AND (
               NEW.proposal_id IS NULL OR
               NEW.reviewer_user_id IS NOT NULL OR
               NEW.proposal_status IS NOT 'PENDING_REVIEW' OR
               NOT EXISTS (
                 SELECT 1 FROM workflow_review_bindings b
                 WHERE b.workflow_id = NEW.workflow_id
                   AND b.proposal_id = NEW.proposal_id
                   AND b.review_id = json_extract(NEW.event_json, '$.review_id')
               )
             ) THEN RAISE(ABORT, 'invalid review-required audit event') END;
             SELECT CASE WHEN NEW.kind = 'HUMAN_REVIEW' AND (
               NEW.proposal_id IS NULL OR
               NEW.reviewer_user_id IS NULL OR
               NEW.proposal_status NOT IN ('APPROVED', 'REJECTED') OR
               NOT EXISTS (
                 SELECT 1 FROM resolution_proposals p
                 WHERE p.proposal_id = NEW.proposal_id
                   AND p.status = NEW.proposal_status
                   AND p.reviewer_user_id = NEW.reviewer_user_id
               ) OR NOT (
                 (EXISTS (
                   SELECT 1 FROM workflow_review_bindings b
                   WHERE b.workflow_id = NEW.workflow_id
                     AND b.proposal_id = NEW.proposal_id
                     AND b.review_id = json_extract(NEW.event_json, '$.review_id')
                 )) OR (
                   NOT EXISTS (
                     SELECT 1 FROM workflow_review_bindings b
                     WHERE b.proposal_id = NEW.proposal_id
                   ) AND json_extract(NEW.event_json, '$.review_id') IS NULL
                 )
               )
             ) THEN RAISE(ABORT, 'invalid human-review audit event') END;
           END""",
    )),
    Migration(7, (
        # SQLite cannot alter a CHECK constraint in place.  Rebuild the parent
        # table within the existing migration transaction so existing child
        # foreign keys remain valid when the replacement takes its name.
        """CREATE TABLE support_cases_v7 (
            case_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            order_id TEXT NOT NULL REFERENCES orders(order_id),
            subject TEXT NOT NULL CHECK(length(trim(subject)) BETWEEN 1 AND 200),
            description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 4000),
            status TEXT NOT NULL CHECK(status IN ('open', 'in_review', 'escalated', 'resolved')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """INSERT INTO support_cases_v7
           (case_id, customer_id, order_id, subject, description, status, created_at, updated_at)
           SELECT case_id, customer_id, order_id, subject, description, status, created_at, updated_at
           FROM support_cases""",
        "DROP TRIGGER validate_delivery_fact_bindings",
        "DROP TABLE support_cases",
        "ALTER TABLE support_cases_v7 RENAME TO support_cases",
        "CREATE INDEX support_cases_customer ON support_cases(customer_id, updated_at, case_id)",
        "CREATE INDEX support_cases_order ON support_cases(order_id, case_id)",
        """CREATE TRIGGER validate_delivery_fact_bindings
           BEFORE INSERT ON delivery_facts BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM support_cases c
               JOIN orders o ON o.order_id = c.order_id
               JOIN order_items i ON i.order_id = o.order_id
               WHERE c.case_id = NEW.case_id
                 AND c.order_id = NEW.order_id
                 AND c.customer_id = NEW.customer_id
                 AND o.customer_id = NEW.customer_id
                 AND i.sku = NEW.sku
                 AND NEW.quantity <= i.quantity
             ) THEN RAISE(ABORT, 'invalid delivery fact bindings') END;
           END""",
    ), requires_foreign_keys_off=True),
    Migration(8, (
        """CREATE TABLE case_resolution_decisions (
            decision_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            action TEXT NOT NULL CHECK(action IN
                ('APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT', 'REQUEST_MORE_EVIDENCE')),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
            customer_message TEXT NOT NULL CHECK(length(trim(customer_message)) BETWEEN 1 AND 2000),
            admin_user_id TEXT NOT NULL CHECK(length(trim(admin_user_id)) BETWEEN 1 AND 120),
            decided_at TEXT NOT NULL,
            UNIQUE(case_id, sequence)
        )""",
        """CREATE INDEX case_resolution_decisions_case
           ON case_resolution_decisions(case_id, sequence)""",
        """CREATE TRIGGER validate_case_resolution_decision_insert
           BEFORE INSERT ON case_resolution_decisions BEGIN
             SELECT CASE WHEN NEW.sequence <> coalesce((
               SELECT max(sequence) + 1 FROM case_resolution_decisions
               WHERE case_id = NEW.case_id
             ), 1)
             THEN RAISE(ABORT, 'invalid case resolution decision sequence') END;
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM support_cases c
               WHERE c.case_id = NEW.case_id AND (
                 (NEW.action = 'REQUEST_MORE_EVIDENCE' AND c.status = 'in_review') OR
                 (NEW.action IN ('APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT')
                  AND c.status = 'resolved')
               )
             ) THEN RAISE(ABORT, 'invalid case resolution decision status') END;
           END""",
        """CREATE TRIGGER immutable_case_resolution_decisions_update
           BEFORE UPDATE ON case_resolution_decisions BEGIN
             SELECT RAISE(ABORT, 'case resolution decisions are immutable');
           END""",
        """CREATE TRIGGER immutable_case_resolution_decisions_delete
           BEFORE DELETE ON case_resolution_decisions BEGIN
             SELECT RAISE(ABORT, 'case resolution decisions are immutable');
           END""",
    )),
    Migration(9, (
        """CREATE TABLE langgraph_checkpoint_staged_writes (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
            checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            write_index INTEGER NOT NULL,
            channel TEXT NOT NULL,
            value_type TEXT NOT NULL,
            value_blob BLOB NOT NULL,
            value_hash TEXT NOT NULL CHECK(length(value_hash) = 64),
            task_path TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index)
        )""",
        """CREATE INDEX langgraph_checkpoint_staged_writes_scope
           ON langgraph_checkpoint_staged_writes(thread_id, checkpoint_ns, checkpoint_id)""",
    )),
    Migration(10, (
        """CREATE TABLE investigation_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT UNIQUE NOT NULL REFERENCES support_cases(case_id),
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            owner_user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            status TEXT NOT NULL CHECK(status = 'PENDING'),
            created_at TEXT NOT NULL,
            UNIQUE(customer_id, idempotency_key)
        )""",
        "CREATE INDEX investigation_jobs_case ON investigation_jobs(case_id)",
        "CREATE INDEX investigation_jobs_pending ON investigation_jobs(status, created_at, job_id)",
    )),
    Migration(11, (
        """CREATE TABLE investigation_jobs_v11 (
            job_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT UNIQUE NOT NULL REFERENCES support_cases(case_id),
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            owner_user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            status TEXT NOT NULL CHECK(status IN ('PENDING', 'CLAIMED', 'RECOVERY_REQUIRED')),
            claim_id TEXT,
            claimed_by TEXT,
            claimed_at TEXT,
            lease_expires_at TEXT,
            recovery_reason TEXT CHECK(recovery_reason IS NULL OR recovery_reason IN ('LEASE_EXPIRED', 'CLAIM_AMBIGUOUS')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(customer_id, idempotency_key),
            CHECK(
                (status = 'PENDING' AND claim_id IS NULL AND claimed_by IS NULL
                 AND claimed_at IS NULL AND lease_expires_at IS NULL AND recovery_reason IS NULL)
                OR
                (status = 'CLAIMED' AND claim_id IS NOT NULL AND claimed_by IS NOT NULL
                 AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND recovery_reason IS NULL)
                OR
                (status = 'RECOVERY_REQUIRED' AND claim_id IS NOT NULL AND claimed_by IS NOT NULL
                 AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND recovery_reason IS NOT NULL)
            )
        )""",
        """INSERT INTO investigation_jobs_v11
           (job_id, case_id, customer_id, owner_user_id, idempotency_key, request_fingerprint,
            status, claim_id, claimed_by, claimed_at, lease_expires_at, recovery_reason,
            created_at, updated_at)
           SELECT job_id, case_id, customer_id, owner_user_id, idempotency_key, request_fingerprint,
                  status, NULL, NULL, NULL, NULL, NULL, created_at, created_at
             FROM investigation_jobs""",
        "DROP TABLE investigation_jobs",
        "ALTER TABLE investigation_jobs_v11 RENAME TO investigation_jobs",
        "CREATE INDEX investigation_jobs_case ON investigation_jobs(case_id)",
        "CREATE INDEX investigation_jobs_pending ON investigation_jobs(status, created_at, job_id)",
        "CREATE INDEX investigation_jobs_claimed ON investigation_jobs(status, lease_expires_at, job_id)",
    )),
    Migration(12, (
        "ALTER TABLE investigation_jobs ADD COLUMN workflow_id TEXT REFERENCES workflow_runs(workflow_id)",
        "ALTER TABLE investigation_jobs ADD COLUMN bound_at TEXT",
        """CREATE UNIQUE INDEX investigation_jobs_workflow
           ON investigation_jobs(workflow_id) WHERE workflow_id IS NOT NULL""",
    )),
    Migration(13, (
        "ALTER TABLE investigation_jobs ADD COLUMN agent_request_json TEXT",
        """ALTER TABLE investigation_jobs ADD COLUMN agent_request_fingerprint TEXT
           CHECK(agent_request_fingerprint IS NULL OR length(agent_request_fingerprint) = 64)""",
    )),
    Migration(14, (
        """CREATE TRIGGER immutable_investigation_job_request_descriptor_update
           BEFORE UPDATE OF agent_request_json, agent_request_fingerprint ON investigation_jobs
           WHEN OLD.agent_request_json IS NOT NULL OR OLD.agent_request_fingerprint IS NOT NULL
           BEGIN
             SELECT RAISE(ABORT, 'investigation request descriptor is immutable');
           END""",
    )),
    Migration(15, (
        "ALTER TABLE investigation_jobs ADD COLUMN execution_attempt_id TEXT",
        "ALTER TABLE investigation_jobs ADD COLUMN execution_attempted_at TEXT",
        """CREATE TRIGGER validate_investigation_job_execution_attempt_pair
           BEFORE UPDATE OF execution_attempt_id, execution_attempted_at ON investigation_jobs
           WHEN (NEW.execution_attempt_id IS NULL) <> (NEW.execution_attempted_at IS NULL)
           BEGIN
             SELECT RAISE(ABORT, 'investigation execution attempt metadata must be paired');
           END""",
        """CREATE TRIGGER immutable_investigation_job_execution_attempt_update
           BEFORE UPDATE OF execution_attempt_id, execution_attempted_at ON investigation_jobs
           WHEN OLD.execution_attempt_id IS NOT NULL OR OLD.execution_attempted_at IS NOT NULL
           BEGIN
             SELECT RAISE(ABORT, 'investigation execution attempt is immutable');
           END""",
    )),
    Migration(16, (
        "DROP TRIGGER validate_case_resolution_decision_insert",
        "DROP TRIGGER immutable_case_resolution_decisions_update",
        "DROP TRIGGER immutable_case_resolution_decisions_delete",
        """CREATE TABLE case_resolution_decisions_v16 (
            decision_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            action TEXT NOT NULL CHECK(action IN
                ('APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT', 'REQUEST_MORE_EVIDENCE', 'ESCALATE')),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
            customer_message TEXT NOT NULL CHECK(length(trim(customer_message)) BETWEEN 1 AND 2000),
            admin_user_id TEXT NOT NULL CHECK(length(trim(admin_user_id)) BETWEEN 1 AND 120),
            decided_at TEXT NOT NULL,
            UNIQUE(case_id, sequence)
        )""",
        """INSERT INTO case_resolution_decisions_v16
           SELECT decision_id, case_id, sequence, action, reason, customer_message,
                  admin_user_id, decided_at
           FROM case_resolution_decisions""",
        "DROP TABLE case_resolution_decisions",
        "ALTER TABLE case_resolution_decisions_v16 RENAME TO case_resolution_decisions",
        """CREATE INDEX case_resolution_decisions_case
           ON case_resolution_decisions(case_id, sequence)""",
        """CREATE TRIGGER validate_case_resolution_decision_insert
           BEFORE INSERT ON case_resolution_decisions BEGIN
             SELECT CASE WHEN NEW.sequence <> coalesce((
               SELECT max(sequence) + 1 FROM case_resolution_decisions
               WHERE case_id = NEW.case_id
             ), 1)
             THEN RAISE(ABORT, 'invalid case resolution decision sequence') END;
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1 FROM support_cases c
               WHERE c.case_id = NEW.case_id AND (
                 (NEW.action = 'REQUEST_MORE_EVIDENCE' AND c.status = 'in_review') OR
                 (NEW.action IN ('APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT') AND c.status = 'resolved') OR
                 (NEW.action = 'ESCALATE' AND c.status = 'escalated')
               )
             ) THEN RAISE(ABORT, 'invalid case resolution decision status') END;
           END""",
        """CREATE TRIGGER immutable_case_resolution_decisions_update
           BEFORE UPDATE ON case_resolution_decisions BEGIN
             SELECT RAISE(ABORT, 'case resolution decisions are immutable');
           END""",
        """CREATE TRIGGER immutable_case_resolution_decisions_delete
           BEFORE DELETE ON case_resolution_decisions BEGIN
             SELECT RAISE(ABORT, 'case resolution decisions are immutable');
           END""",
    )),
    Migration(17, (
        """CREATE TABLE case_customer_communications (
            communication_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            message TEXT NOT NULL CHECK(length(trim(message)) BETWEEN 1 AND 2000),
            admin_user_id TEXT NOT NULL CHECK(length(admin_user_id) = 36),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            sent_at TEXT NOT NULL,
            UNIQUE(case_id, sequence),
            UNIQUE(admin_user_id, idempotency_key)
        )""",
        """CREATE INDEX case_customer_communications_case
           ON case_customer_communications(case_id, sequence)""",
        """CREATE TRIGGER validate_case_customer_communication_insert
           BEFORE INSERT ON case_customer_communications BEGIN
             SELECT CASE WHEN NEW.sequence <> coalesce((
               SELECT max(sequence) + 1 FROM case_customer_communications
               WHERE case_id = NEW.case_id
             ), 1)
             THEN RAISE(ABORT, 'invalid customer communication sequence') END;
           END""",
        """CREATE TRIGGER immutable_case_customer_communications_update
           BEFORE UPDATE ON case_customer_communications BEGIN
             SELECT RAISE(ABORT, 'customer communications are immutable');
           END""",
        """CREATE TRIGGER immutable_case_customer_communications_delete
           BEFORE DELETE ON case_customer_communications BEGIN
             SELECT RAISE(ABORT, 'customer communications are immutable');
           END""",
    )),
    Migration(18, (
        """CREATE TABLE IF NOT EXISTS investigation_control (
            singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
            mode TEXT NOT NULL CHECK(mode IN ('MANUAL', 'AUTO')),
            version INTEGER NOT NULL CHECK(version >= 1),
            updated_by TEXT CHECK(updated_by IS NULL OR length(updated_by) = 36),
            updated_at TEXT NOT NULL
        )""",
        """INSERT OR IGNORE INTO investigation_control
           (singleton, mode, version, updated_by, updated_at)
           VALUES (1, 'MANUAL', 1, NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
        """CREATE TABLE IF NOT EXISTS investigation_mode_audit (
            sequence INTEGER PRIMARY KEY NOT NULL CHECK(sequence >= 1),
            previous_mode TEXT CHECK(previous_mode IS NULL OR previous_mode IN ('MANUAL', 'AUTO')),
            mode TEXT NOT NULL CHECK(mode IN ('MANUAL', 'AUTO')),
            version INTEGER NOT NULL CHECK(version >= 1),
            admin_user_id TEXT CHECK(admin_user_id IS NULL OR length(admin_user_id) = 36),
            idempotency_key TEXT CHECK(idempotency_key IS NULL OR length(idempotency_key) = 36),
            request_fingerprint TEXT CHECK(request_fingerprint IS NULL OR length(request_fingerprint) = 64),
            changed_at TEXT NOT NULL,
            UNIQUE(admin_user_id, idempotency_key)
        )""",
        """INSERT OR IGNORE INTO investigation_mode_audit
           (sequence, previous_mode, mode, version, admin_user_id, idempotency_key,
            request_fingerprint, changed_at)
           SELECT 1, NULL, mode, version, NULL, NULL, NULL, updated_at
             FROM investigation_control WHERE singleton = 1""",
        """CREATE TRIGGER IF NOT EXISTS immutable_investigation_mode_audit_update
           BEFORE UPDATE ON investigation_mode_audit BEGIN
             SELECT RAISE(ABORT, 'investigation mode audit is immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS immutable_investigation_mode_audit_delete
           BEFORE DELETE ON investigation_mode_audit BEGIN
             SELECT RAISE(ABORT, 'investigation mode audit is immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS immutable_investigation_control_delete
           BEFORE DELETE ON investigation_control BEGIN
             SELECT RAISE(ABORT, 'investigation control cannot be deleted');
           END""",
        """CREATE TABLE IF NOT EXISTS investigation_start_requests (
            admin_user_id TEXT NOT NULL CHECK(length(admin_user_id) = 36),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
            job_id TEXT UNIQUE NOT NULL REFERENCES investigation_jobs(job_id),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            requested_at TEXT NOT NULL,
            PRIMARY KEY(admin_user_id, idempotency_key)
        )""",
        """CREATE TRIGGER IF NOT EXISTS immutable_investigation_start_requests_update
           BEFORE UPDATE ON investigation_start_requests BEGIN
             SELECT RAISE(ABORT, 'investigation start requests are immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS immutable_investigation_start_requests_delete
           BEFORE DELETE ON investigation_start_requests BEGIN
             SELECT RAISE(ABORT, 'investigation start requests are immutable');
           END""",
        """ALTER TABLE investigation_jobs ADD COLUMN submission_mode TEXT NOT NULL
           DEFAULT 'MANUAL' CHECK(submission_mode IN ('MANUAL', 'AUTO'))""",
        """ALTER TABLE investigation_jobs ADD COLUMN mode_version INTEGER NOT NULL
           DEFAULT 1 CHECK(mode_version >= 1)""",
        """CREATE TRIGGER immutable_investigation_job_mode_snapshot_update
           BEFORE UPDATE OF submission_mode, mode_version ON investigation_jobs BEGIN
             SELECT RAISE(ABORT, 'investigation job mode snapshot is immutable');
           END""",
        """CREATE INDEX IF NOT EXISTS investigation_jobs_submission_mode
           ON investigation_jobs(submission_mode, status, created_at, job_id)""",
    )),
    Migration(19, (
        """CREATE TABLE IF NOT EXISTS cognito_customer_bindings (
            subject_id TEXT PRIMARY KEY NOT NULL CHECK(length(subject_id) = 36),
            customer_id TEXT UNIQUE NOT NULL REFERENCES customers(customer_id),
            created_at TEXT NOT NULL
        )""",
        """CREATE TRIGGER IF NOT EXISTS immutable_cognito_customer_binding_update
           BEFORE UPDATE ON cognito_customer_bindings BEGIN
             SELECT RAISE(ABORT, 'cognito customer bindings are immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS immutable_cognito_customer_binding_delete
           BEFORE DELETE ON cognito_customer_bindings BEGIN
             SELECT RAISE(ABORT, 'cognito customer bindings are immutable');
           END""",
    )),
    Migration(20, (
        """ALTER TABLE workflow_runs ADD COLUMN failure_category TEXT
           CHECK(failure_category IS NULL OR failure_category IN
             ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
              'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR','WORKFLOW_ERROR'))""",
        """ALTER TABLE workflow_runs ADD COLUMN failure_stage TEXT
           CHECK(failure_stage IS NULL OR failure_stage IN ('AGENT','WORKFLOW'))""",
        "DROP TRIGGER validate_workflow_run_update",
        """CREATE TRIGGER validate_workflow_run_update
           BEFORE UPDATE ON workflow_runs BEGIN
             SELECT CASE WHEN
               OLD.workflow_id IS NOT NEW.workflow_id OR
               OLD.run_id IS NOT NEW.run_id OR
               OLD.case_id IS NOT NEW.case_id OR
               OLD.owner_user_id IS NOT NEW.owner_user_id OR
               OLD.request_id IS NOT NEW.request_id OR
               OLD.request_fingerprint IS NOT NEW.request_fingerprint OR
               OLD.provider IS NOT NEW.provider OR
               OLD.created_at IS NOT NEW.created_at OR
               NEW.updated_at <= OLD.updated_at OR
               NOT (
                 (OLD.status = 'RUNNING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN
                   ('RESUMING', 'REVIEWED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'RESUMING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'REVIEWED', 'FAILED', 'RECOVERY_REQUIRED'))
               ) OR
               (NEW.status = 'FAILED' AND NOT (
                 (NEW.failure_stage = 'WORKFLOW' AND NEW.failure_category = 'WORKFLOW_ERROR') OR
                 (NEW.failure_stage = 'AGENT' AND NEW.failure_category IN
                   ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
                    'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR'))
               )) OR
               (NEW.status <> 'FAILED' AND
                 (NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL))
             THEN RAISE(ABORT, 'invalid workflow status transition') END;
           END""",
    )),
    Migration(21, (
        """ALTER TABLE workflow_runs ADD COLUMN invalid_response_stage TEXT
           CHECK(invalid_response_stage IS NULL OR invalid_response_stage IN
             ('provider_envelope','json_guardrail','output_schema'))""",
        """ALTER TABLE workflow_runs ADD COLUMN invalid_response_category TEXT
           CHECK(invalid_response_category IS NULL OR invalid_response_category IN
             ('bedrock_empty_output','bedrock_nonempty_json_rejected'))""",
        "DROP TRIGGER validate_workflow_run_update",
        """CREATE TRIGGER validate_workflow_run_update
           BEFORE UPDATE ON workflow_runs BEGIN
             SELECT CASE WHEN
               OLD.workflow_id IS NOT NEW.workflow_id OR
               OLD.run_id IS NOT NEW.run_id OR
               OLD.case_id IS NOT NEW.case_id OR
               OLD.owner_user_id IS NOT NEW.owner_user_id OR
               OLD.request_id IS NOT NEW.request_id OR
               OLD.request_fingerprint IS NOT NEW.request_fingerprint OR
               OLD.provider IS NOT NEW.provider OR
               OLD.created_at IS NOT NEW.created_at OR
               NEW.updated_at <= OLD.updated_at OR
               NOT (
                 (OLD.status = 'RUNNING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN
                   ('RESUMING', 'REVIEWED', 'RECOVERY_REQUIRED')) OR
                 (OLD.status = 'RESUMING' AND NEW.status IN
                   ('REVIEW_REQUIRED', 'REVIEWED', 'FAILED', 'RECOVERY_REQUIRED'))
               ) OR
               (NEW.status = 'FAILED' AND NOT (
                 (NEW.failure_stage = 'WORKFLOW' AND NEW.failure_category = 'WORKFLOW_ERROR') OR
                 (NEW.failure_stage = 'AGENT' AND NEW.failure_category IN
                   ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
                    'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR'))
               )) OR
               (NEW.status <> 'FAILED' AND
                 (NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL OR
                  NEW.invalid_response_stage IS NOT NULL OR NEW.invalid_response_category IS NOT NULL)) OR
               ((NEW.invalid_response_stage IS NOT NULL OR NEW.invalid_response_category IS NOT NULL) AND NOT (
                 NEW.failure_category = 'INVALID_RESPONSE' AND NEW.failure_stage = 'AGENT' AND
                 NEW.invalid_response_stage IN ('provider_envelope','json_guardrail','output_schema') AND
                 (NEW.invalid_response_category IS NULL OR
                  (NEW.invalid_response_stage = 'json_guardrail' AND NEW.invalid_response_category IN
                    ('bedrock_empty_output','bedrock_nonempty_json_rejected')))
               ))
             THEN RAISE(ABORT, 'invalid workflow status transition') END;
           END""",
    )),
)


class SQLiteUnitOfWork:
    """Short-lived SQLite transactions following the conversation-store convention."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except PersistenceError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


class SQLitePersistence(SQLiteUnitOfWork):
    """Versioned SQLite foundation. It deliberately owns no application service wiring."""

    def __init__(self, path: str | Path, *, initialize: bool = False) -> None:
        super().__init__(path)
        try:
            if initialize:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.touch(exist_ok=True)
            if not self._path.is_file():
                raise PersistenceError("PERSISTENCE_UNAVAILABLE")
            self._migrate()
            self._validate()
        except PersistenceError:
            raise
        except Exception:
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None

    def _migrate(self) -> None:
        connection = None
        try:
            connection = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=5)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL)")
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            known = {migration.version for migration in MIGRATIONS}
            if applied - known:
                raise PersistenceError("PERSISTENCE_UNAVAILABLE")
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                if migration.requires_foreign_keys_off:
                    # SQLite cannot rebuild a referenced parent table with
                    # enforcement enabled. Validate within the migration
                    # transaction, then restore enforcement before returning.
                    connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        connection.execute(statement)
                    if (migration.requires_foreign_keys_off
                            and connection.execute("PRAGMA foreign_key_check").fetchone() is not None):
                        raise PersistenceError("PERSISTENCE_UNAVAILABLE")
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                        (migration.version,),
                    )
                    connection.commit()
                finally:
                    if migration.requires_foreign_keys_off:
                        connection.execute("PRAGMA foreign_keys=ON")
        except PersistenceError:
            if connection is not None:
                connection.rollback()
            raise
        except Exception:
            if connection is not None:
                connection.rollback()
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    def _validate(self) -> None:
        try:
            with self.transaction() as connection:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise PersistenceError("PERSISTENCE_UNAVAILABLE")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise PersistenceError("PERSISTENCE_UNAVAILABLE")
                versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
                if versions != [migration.version for migration in MIGRATIONS]:
                    raise PersistenceError("PERSISTENCE_UNAVAILABLE")
        except PersistenceError:
            raise


class SQLiteCaseRepository:
    """Durable customer, order, and case repository built on the M5B UoW."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _customer(row: sqlite3.Row) -> Customer:
        return Customer.model_validate({"id": row["customer_id"], "name": row["name"]})

    @staticmethod
    def _case(row: sqlite3.Row) -> SupportCase:
        return SupportCase.model_validate({
            "id": row["case_id"], "customer_id": row["customer_id"], "order_id": row["order_id"],
            "subject": row["subject"], "description": row["description"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })

    @staticmethod
    def _decision(row: sqlite3.Row) -> CaseResolutionDecision:
        try:
            return CaseResolutionDecision.model_validate({
                "id": row["decision_id"], "case_id": row["case_id"],
                "action": row["action"], "reason": row["reason"],
                "customer_message": row["customer_message"],
                "admin_user_id": row["admin_user_id"], "decided_at": row["decided_at"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _customer_communication(row: sqlite3.Row) -> CaseCustomerCommunication:
        try:
            return CaseCustomerCommunication.model_validate({
                "id": row["communication_id"], "case_id": row["case_id"],
                "message": row["message"], "admin_user_id": row["admin_user_id"],
                "sent_at": row["sent_at"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _order(connection: sqlite3.Connection, row: sqlite3.Row) -> Order:
        # SQLite's rowid preserves the legacy insertion order.  The
        # PostgreSQL cursor facade has no rowid, so use the primary-key SKU
        # ordering there for a stable, portable result.
        order_by = "rowid" if getattr(connection, "supports_rowid", True) else "sku"
        items = connection.execute(
            f"SELECT sku, name, quantity FROM order_items WHERE order_id = ? ORDER BY {order_by}",
            (row["order_id"],),
        ).fetchall()
        return Order.model_validate({
            "id": row["order_id"], "customer_id": row["customer_id"],
            "items": [OrderItem.model_validate(dict(item)) for item in items],
        })

    @staticmethod
    def _require_customer(connection: sqlite3.Connection, customer_id: UUID) -> sqlite3.Row:
        row = connection.execute(
            "SELECT customer_id, name FROM customers WHERE customer_id = ?", (str(customer_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError("Customer not found")
        return row

    @classmethod
    def _require_order(cls, connection: sqlite3.Connection, order_id: UUID) -> Order:
        row = connection.execute(
            "SELECT order_id, customer_id FROM orders WHERE order_id = ?", (str(order_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError("Order not found")
        return cls._order(connection, row)

    def seed_customer(self, customer: Customer) -> None:
        with self._persistence.transaction() as connection:
            existing = connection.execute(
                "SELECT customer_id, name FROM customers WHERE customer_id = ?", (str(customer.id),)
            ).fetchone()
            if existing is not None:
                if self._customer(existing) != customer:
                    raise ValueError(
                        f"Startup seed conflict: customer {customer.id} already exists with different data"
                    )
                return
            connection.execute("INSERT INTO customers(customer_id, name) VALUES (?, ?)",
                               (str(customer.id), customer.name))

    def create_customer(self, customer: Customer) -> Customer:
        with self._persistence.transaction() as connection:
            existing = connection.execute("SELECT 1 FROM customers WHERE customer_id = ?", (str(customer.id),)).fetchone()
            if existing is not None:
                raise ConflictError("Customer already exists")
            connection.execute("INSERT INTO customers(customer_id, name) VALUES (?, ?)",
                               (str(customer.id), customer.name))
        return customer

    def get_customer(self, customer_id: UUID) -> Customer:
        with self._persistence.transaction() as connection:
            return self._customer(self._require_customer(connection, customer_id))

    def create_order(self, order: Order) -> Order:
        skus = [item.sku for item in order.items]
        if len(skus) != len(set(skus)):
            raise ConflictError("An order cannot contain duplicate SKUs")
        with self._persistence.transaction() as connection:
            self._require_customer(connection, order.customer_id)
            if connection.execute("SELECT 1 FROM orders WHERE order_id = ?", (str(order.id),)).fetchone() is not None:
                raise ConflictError("Order already exists")
            connection.execute(
                "INSERT INTO orders(order_id, customer_id, created_at) VALUES (?, ?, ?)",
                (str(order.id), str(order.customer_id), datetime.now(timezone.utc).isoformat()),
            )
            connection.executemany(
                "INSERT INTO order_items(order_id, sku, name, quantity) VALUES (?, ?, ?, ?)",
                [(str(order.id), item.sku, item.name, item.quantity) for item in order.items],
            )
        return order

    def get_order(self, order_id: UUID) -> Order:
        with self._persistence.transaction() as connection:
            return self._require_order(connection, order_id)

    def create_case(self, case: SupportCase) -> SupportCase:
        with self._persistence.transaction() as connection:
            self._require_customer(connection, case.customer_id)
            order = self._require_order(connection, case.order_id)
            if order.customer_id != case.customer_id:
                raise ConflictError("Order does not belong to the selected customer")
            if connection.execute("SELECT 1 FROM support_cases WHERE case_id = ?", (str(case.id),)).fetchone() is not None:
                raise ConflictError("Case already exists")
            connection.execute(
                """INSERT INTO support_cases
                   (case_id, customer_id, order_id, subject, description, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(case.id), str(case.customer_id), str(case.order_id), case.subject, case.description,
                 case.status.value, case.created_at.isoformat(), case.updated_at.isoformat()),
            )
        return case

    def get_case(self, case_id: UUID) -> SupportCase:
        with self._persistence.transaction() as connection:
            row = connection.execute("SELECT * FROM support_cases WHERE case_id = ?", (str(case_id),)).fetchone()
            if row is None:
                raise NotFoundError("Case not found")
            return self._case(row)

    def list_orders(self, customer_id: UUID | None = None) -> list[Order]:
        with self._persistence.transaction() as connection:
            if customer_id is None:
                rows = connection.execute("SELECT order_id, customer_id FROM orders ORDER BY created_at, order_id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT order_id, customer_id FROM orders WHERE customer_id = ? ORDER BY created_at, order_id",
                    (str(customer_id),),
                ).fetchall()
            return [self._order(connection, row) for row in rows]

    def list_cases(self, customer_id: UUID | None = None) -> list[SupportCase]:
        with self._persistence.transaction() as connection:
            if customer_id is None:
                rows = connection.execute("SELECT * FROM support_cases ORDER BY created_at, case_id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM support_cases WHERE customer_id = ? ORDER BY created_at, case_id",
                    (str(customer_id),),
                ).fetchall()
            return [self._case(row) for row in rows]

    def update_case(self, case: SupportCase) -> SupportCase:
        with self._persistence.transaction() as connection:
            updated = connection.execute(
                "UPDATE support_cases SET status = ?, updated_at = ? WHERE case_id = ?",
                (case.status.value, case.updated_at.isoformat(), str(case.id)),
            )
            if updated.rowcount != 1:
                raise NotFoundError("Case not found")
        return case

    def resolution_history(self, case_id: UUID) -> tuple[CaseResolutionDecision, ...]:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM support_cases WHERE case_id = ?", (str(case_id),)
            ).fetchone()
            if row is None:
                raise NotFoundError("Case not found")
            rows = connection.execute(
                """SELECT * FROM case_resolution_decisions
                   WHERE case_id = ? ORDER BY sequence""",
                (str(case_id),),
            ).fetchall()
            return tuple(self._decision(row) for row in rows)

    def record_resolution(self, current: SupportCase, updated: SupportCase,
                          decision: CaseResolutionDecision) -> tuple[SupportCase, CaseResolutionDecision]:
        """Persist one validated admin decision and its case status atomically."""
        if (current.id != updated.id or current.id != decision.case_id
                or current.customer_id != updated.customer_id
                or current.order_id != updated.order_id
                or current.subject != updated.subject
                or current.description != updated.description
                or not can_transition(current.status, updated.status)):
            raise ConflictError("Case resolution does not match the current case")
        if decision.action == ResolutionAction.REQUEST_MORE_EVIDENCE:
            valid_outcome = (current.status in (CaseStatus.OPEN, CaseStatus.IN_REVIEW)
                             and updated.status == CaseStatus.IN_REVIEW)
        elif decision.action == ResolutionAction.ESCALATE:
            valid_outcome = (current.status in (CaseStatus.OPEN, CaseStatus.IN_REVIEW)
                             and updated.status == CaseStatus.ESCALATED)
        else:
            valid_outcome = (current.status == CaseStatus.IN_REVIEW
                             and updated.status == CaseStatus.RESOLVED)
        if not valid_outcome:
            raise ConflictError("Invalid case resolution transition")

        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM support_cases WHERE case_id = ?", (str(current.id),)
            ).fetchone()
            if row is None:
                raise NotFoundError("Case not found")
            if self._case(row) != current:
                raise ConflictError("Case resolution is stale")
            latest = connection.execute(
                """SELECT action, reason, customer_message FROM case_resolution_decisions
                   WHERE case_id = ? ORDER BY sequence DESC LIMIT 1""",
                (str(current.id),),
            ).fetchone()
            if latest is not None and (latest["action"], latest["reason"], latest["customer_message"]) == (
                    decision.action.value, decision.reason, decision.customer_message):
                raise ConflictError("Duplicate case decision")
            status_update = connection.execute(
                """UPDATE support_cases SET status = ?, updated_at = ?
                   WHERE case_id = ? AND status = ? AND updated_at = ?""",
                (updated.status.value, updated.updated_at.isoformat(), str(current.id),
                 current.status.value, current.updated_at.isoformat()),
            )
            if status_update.rowcount != 1:
                raise ConflictError("Case resolution is stale")
            sequence = connection.execute(
                """SELECT coalesce(max(sequence), 0) + 1
                   FROM case_resolution_decisions WHERE case_id = ?""",
                (str(current.id),),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO case_resolution_decisions
                   (decision_id, case_id, sequence, action, reason, customer_message,
                    admin_user_id, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(decision.id), str(decision.case_id), sequence, decision.action.value,
                 decision.reason, decision.customer_message, str(decision.admin_user_id),
                 decision.decided_at.isoformat()),
            )
        return updated, decision

    def customer_communication_history(self, case_id: UUID) -> tuple[CaseCustomerCommunication, ...]:
        with self._persistence.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM support_cases WHERE case_id = ?", (str(case_id),)
            ).fetchone() is None:
                raise NotFoundError("Case not found")
            rows = connection.execute(
                """SELECT * FROM case_customer_communications
                   WHERE case_id = ? ORDER BY sequence""",
                (str(case_id),),
            ).fetchall()
            return tuple(self._customer_communication(row) for row in rows)

    def record_customer_communication(
        self, communication: CaseCustomerCommunication, *, idempotency_key: UUID,
        request_fingerprint: str,
    ) -> tuple[CaseCustomerCommunication, bool]:
        """Append an immutable audit record; a matching retry returns the original row."""
        with self._persistence.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM support_cases WHERE case_id = ?", (str(communication.case_id),)
            ).fetchone() is None:
                raise NotFoundError("Case not found")
            existing = connection.execute(
                """SELECT * FROM case_customer_communications
                   WHERE admin_user_id = ? AND idempotency_key = ?""",
                (str(communication.admin_user_id), str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise ConflictError(
                        "Idempotency key conflicts with an existing customer communication"
                    )
                return self._customer_communication(existing), False
            sequence = connection.execute(
                """SELECT coalesce(max(sequence), 0) + 1
                   FROM case_customer_communications WHERE case_id = ?""",
                (str(communication.case_id),),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO case_customer_communications
                   (communication_id, case_id, sequence, message, admin_user_id,
                    idempotency_key, request_fingerprint, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(communication.id), str(communication.case_id), sequence,
                 communication.message, str(communication.admin_user_id),
                 str(idempotency_key), request_fingerprint,
                 communication.sent_at.isoformat()),
            )
        return communication, True


class SQLiteCognitoCustomerBindingRepository:
    """Immutable trusted ``sub`` to customer mappings for Cognito identities.

    Bindings are supplied by a trusted deployment configuration at startup, not
    by a request or token claim.  An existing mapping may be replayed during a
    rolling deployment, but it can never be reassigned to another customer.
    """

    def __init__(self, persistence: UnitOfWork) -> None:
        self._persistence = persistence

    def bind(self, binding: CognitoCustomerBinding) -> CognitoCustomerBinding:
        binding = CognitoCustomerBinding.model_validate(binding)
        with self._persistence.transaction() as connection:
            SQLiteCaseRepository._require_customer(connection, binding.customer_id)
            existing = connection.execute(
                "SELECT customer_id FROM cognito_customer_bindings WHERE subject_id = ?",
                (str(binding.subject_id),),
            ).fetchone()
            if existing is not None:
                if existing["customer_id"] != str(binding.customer_id):
                    raise ConflictError("Cognito subject is already bound to another customer")
                return binding
            owner = connection.execute(
                "SELECT subject_id FROM cognito_customer_bindings WHERE customer_id = ?",
                (str(binding.customer_id),),
            ).fetchone()
            if owner is not None:
                raise ConflictError("Customer is already bound to another Cognito subject")
            connection.execute(
                """INSERT INTO cognito_customer_bindings(subject_id, customer_id, created_at)
                   VALUES (?, ?, ?)""",
                (str(binding.subject_id), str(binding.customer_id),
                 datetime.now(timezone.utc).isoformat()),
            )
        return binding

    def customer_id_for_subject(self, subject_id: UUID) -> UUID | None:
        if not isinstance(subject_id, UUID):
            return None
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT customer_id FROM cognito_customer_bindings WHERE subject_id = ?",
                (str(subject_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            customer_id = UUID(row["customer_id"])
            if str(customer_id) != row["customer_id"]:
                raise ValueError("Non-canonical customer ID")
            return customer_id
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None


class SQLiteInvestigationControlRepository:
    """Singleton mode control plus immutable administrative audit history."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _control(row: sqlite3.Row) -> InvestigationControl:
        try:
            return InvestigationControl.model_validate({
                "mode": row["mode"], "version": row["version"],
                "updated_by": row["updated_by"],
                "updated_at": _canonical_legacy_postgres_timestamp(row["updated_at"]),
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _event(row: sqlite3.Row) -> InvestigationModeAuditEvent:
        try:
            return InvestigationModeAuditEvent.model_validate({
                "sequence": row["sequence"], "previous_mode": row["previous_mode"],
                "mode": row["mode"], "version": row["version"],
                "admin_user_id": row["admin_user_id"],
                "idempotency_key": row["idempotency_key"],
                "changed_at": _canonical_legacy_postgres_timestamp(row["changed_at"]),
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    def get(self) -> InvestigationControl:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_control WHERE singleton = 1",
            ).fetchone()
            if row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return self._control(row)

    def set_mode(self, mode: InvestigationMode, *, expected_version: int,
                 admin_user_id: UUID, idempotency_key: UUID,
                 now: datetime | None = None) -> InvestigationControl:
        """Apply one optimistic, idempotent mode change without dispatching work."""
        mode = InvestigationMode(mode)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        timestamp = datetime.now(timezone.utc) if now is None else now
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Mode change timestamps must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        request_fingerprint = sha256(json.dumps({
            "expected_version": expected_version, "mode": mode.value,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        with self._persistence.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM investigation_mode_audit
                   WHERE admin_user_id = ? AND idempotency_key = ?""",
                (str(admin_user_id), str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise ConflictError("Idempotency key conflicts with an investigation mode request")
                event = self._event(existing)
                return InvestigationControl(
                    mode=event.mode, version=event.version, updated_by=event.admin_user_id,
                    updated_at=event.changed_at,
                )

            row = connection.execute(
                "SELECT * FROM investigation_control WHERE singleton = 1",
            ).fetchone()
            if row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            current = self._control(row)
            if current.version != expected_version:
                raise ConflictError("Investigation mode version is stale")
            if current.mode is mode:
                raise ConflictError("Investigation mode is already selected")
            changed_at = timestamp if timestamp > current.updated_at else current.updated_at + timedelta(microseconds=1)
            next_version = current.version + 1
            updated = connection.execute(
                """UPDATE investigation_control
                   SET mode = ?, version = ?, updated_by = ?, updated_at = ?
                   WHERE singleton = 1 AND version = ?""",
                (mode.value, next_version, str(admin_user_id), changed_at.isoformat(),
                 current.version),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation mode version is stale")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM investigation_mode_audit",
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO investigation_mode_audit
                   (sequence, previous_mode, mode, version, admin_user_id, idempotency_key,
                    request_fingerprint, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sequence, current.mode.value, mode.value, next_version, str(admin_user_id),
                 str(idempotency_key), request_fingerprint, changed_at.isoformat()),
            )
            return InvestigationControl(
                mode=mode, version=next_version, updated_by=admin_user_id,
                updated_at=changed_at,
            )

    def audit(self, *, after: int = 0, limit: int = 100) -> tuple[InvestigationModeAuditEvent, ...]:
        if (isinstance(after, bool) or isinstance(limit, bool) or after < 0
                or limit < 1 or limit > 101):
            raise ValueError("Invalid investigation mode audit page")
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM investigation_mode_audit
                   WHERE sequence > ? ORDER BY sequence LIMIT ?""",
                (after, limit),
            ).fetchall()
            return tuple(self._event(row) for row in rows)

    def status_counts(self) -> dict[str, int]:
        counts = {name: 0 for name in (
            "pending", "running", "review_required", "completed", "failed",
            "recovery_required",
        )}
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                """SELECT j.status AS job_status, w.status AS workflow_status, count(*) AS total
                   FROM investigation_jobs j
                   LEFT JOIN workflow_runs w ON w.workflow_id = j.workflow_id
                   GROUP BY j.status, w.status""",
            ).fetchall()
        for row in rows:
            total = row["total"]
            if row["job_status"] == InvestigationJobStatus.PENDING.value:
                counts["pending"] += total
            elif (row["job_status"] == InvestigationJobStatus.RECOVERY_REQUIRED.value
                  or row["workflow_status"] == WorkflowMetadataStatus.RECOVERY_REQUIRED.value):
                counts["recovery_required"] += total
            elif row["workflow_status"] == WorkflowMetadataStatus.REVIEW_REQUIRED.value:
                counts["review_required"] += total
            elif row["workflow_status"] in (
                WorkflowMetadataStatus.COMPLETED.value, WorkflowMetadataStatus.REVIEWED.value,
            ):
                counts["completed"] += total
            elif row["workflow_status"] == WorkflowMetadataStatus.FAILED.value:
                counts["failed"] += total
            else:
                counts["running"] += total
        counts["total"] = sum(counts.values())
        return counts


class SQLiteInvestigationJobRepository:
    """Durable, idempotent complaint-intake queue; it never dispatches work."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _job(row: sqlite3.Row) -> InvestigationJob:
        try:
            return InvestigationJob.model_validate({
                "id": row["job_id"], "case_id": row["case_id"],
                "customer_id": row["customer_id"], "owner_user_id": row["owner_user_id"],
                "idempotency_key": row["idempotency_key"],
                "request_fingerprint": row["request_fingerprint"],
                "status": row["status"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "claim_id": row["claim_id"],
                "claimed_by": row["claimed_by"], "claimed_at": row["claimed_at"],
                "lease_expires_at": row["lease_expires_at"],
                "recovery_reason": row["recovery_reason"],
                "workflow_id": row["workflow_id"], "bound_at": row["bound_at"],
                "agent_request_json": row["agent_request_json"],
                "agent_request_fingerprint": row["agent_request_fingerprint"],
                "execution_attempt_id": row["execution_attempt_id"],
                "execution_attempted_at": row["execution_attempted_at"],
                "submission_mode": row["submission_mode"],
                "mode_version": row["mode_version"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _claim_time(now: datetime | None) -> datetime:
        timestamp = datetime.now(timezone.utc) if now is None else now
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Claim timestamps must be timezone-aware")
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _execution(job: InvestigationJob) -> InvestigationExecutionIdentity:
        # A job has exactly one durable execution identity. Claim leases never
        # generate a new identity, which prevents accidental replay semantics.
        return InvestigationExecutionIdentity(
            job_id=job.id, execution_id=job.id, customer_id=job.customer_id,
            owner_user_id=job.owner_user_id,
        )

    @staticmethod
    def _canonical_request(job: InvestigationJob, case_row: sqlite3.Row) -> tuple[AgentRequest, str, str]:
        """Build the one server-owned request descriptor for this intake job."""
        if case_row["customer_id"] != str(job.customer_id):
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        request = AgentRequest(
            case_id=job.case_id,
            message=(f"Investigate support case {job.case_id}.\n"
                     f"Subject: {case_row['subject']}\n"
                     f"Description: {case_row['description']}"),
        )
        descriptor = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        )
        return request, descriptor, agent_request_fingerprint(request)

    @staticmethod
    def _stored_request(job: InvestigationJob, run: WorkflowRunMetadata) -> AgentRequest:
        """Validate the immutable descriptor rather than regenerating it."""
        if job.agent_request_json is None or job.agent_request_fingerprint is None:
            raise ValueError("Legacy reservation has no execution descriptor")
        try:
            payload = json.loads(job.agent_request_json)
            descriptor = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            request = AgentRequest.model_validate(payload)
        except Exception:
            raise ValueError("Invalid persisted execution descriptor") from None
        if (descriptor != job.agent_request_json or request.case_id != job.case_id
                or agent_request_fingerprint(request) != job.agent_request_fingerprint
                or run.request_fingerprint != job.agent_request_fingerprint):
            raise ValueError("Persisted execution descriptor does not match its reservation")
        return request

    @classmethod
    def _require_bound_workflow_recovery(cls, connection: sqlite3.Connection, *,
                                         job: InvestigationJob, observed_at: datetime) -> None:
        """Keep a bound reservation non-runnable when its claim becomes stale."""
        if job.workflow_id is None:
            return
        row = connection.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(job.workflow_id),),
        ).fetchone()
        if row is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        run = SQLiteWorkflowMetadataRepository._run(row)
        if (run.workflow_id != job.id or run.run_id != job.id or run.request_id != job.id
                or run.case_id != job.case_id or run.owner_user_id != job.claimed_by):
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        if run.status == WorkflowMetadataStatus.RECOVERY_REQUIRED:
            return
        if run.status not in (
            WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.RESUMING,
            WorkflowMetadataStatus.REVIEW_REQUIRED,
        ):
            return
        updated_at = observed_at if observed_at > run.updated_at else run.updated_at + timedelta(microseconds=1)
        updated = connection.execute(
            """UPDATE workflow_runs SET status = ?, updated_at = ?
               WHERE workflow_id = ? AND status = ?""",
            (WorkflowMetadataStatus.RECOVERY_REQUIRED.value, updated_at.isoformat(),
             str(run.workflow_id), run.status.value),
        )
        if updated.rowcount != 1:
            raise ConflictError("Workflow status is stale")
        recovered = WorkflowRunMetadata.model_validate({
            **run.model_dump(), "status": WorkflowMetadataStatus.RECOVERY_REQUIRED,
            "updated_at": updated_at,
        })
        SQLiteWorkflowMetadataRepository._append_run_event(
            connection, run=recovered, kind="RECOVERY_REQUIRED", previous_status=run.status,
        )

    @classmethod
    def _require_claim_recovery(cls, connection: sqlite3.Connection, *,
                                job: InvestigationJob, observed_at: datetime) -> None:
        cls._require_bound_workflow_recovery(connection, job=job, observed_at=observed_at)
        updated = connection.execute(
            """UPDATE investigation_jobs
               SET status = ?, recovery_reason = ?, updated_at = ?
               WHERE job_id = ? AND status = ?""",
            (InvestigationJobStatus.RECOVERY_REQUIRED.value,
             InvestigationRecoveryReason.CLAIM_AMBIGUOUS.value, observed_at.isoformat(),
             str(job.id), InvestigationJobStatus.CLAIMED.value),
        )
        if updated.rowcount != 1:
            raise ConflictError("Investigation job claim is no longer active")

    @staticmethod
    def fingerprint(case: SupportCase) -> str:
        payload = {
            "order_id": str(case.order_id),
            "subject": case.subject,
            "description": case.description,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def submit(self, case: SupportCase, *, owner_user_id: UUID,
               idempotency_key: UUID) -> tuple[SupportCase, InvestigationJob, bool]:
        """Atomically create a case and pending job, or return the matching prior pair."""
        case = SupportCase.model_validate(case.model_dump())
        fingerprint = self.fingerprint(case)
        with self._persistence.transaction() as connection:
            existing_row = connection.execute(
                """SELECT * FROM investigation_jobs
                   WHERE customer_id = ? AND idempotency_key = ?""",
                (str(case.customer_id), str(idempotency_key)),
            ).fetchone()
            if existing_row is not None:
                existing = self._job(existing_row)
                if existing.request_fingerprint != fingerprint:
                    raise ConflictError("Idempotency key conflicts with an existing intake request")
                case_row = connection.execute(
                    "SELECT * FROM support_cases WHERE case_id = ?", (str(existing.case_id),),
                ).fetchone()
                if case_row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                return SQLiteCaseRepository._case(case_row), existing, False

            SQLiteCaseRepository._require_customer(connection, case.customer_id)
            order = SQLiteCaseRepository._require_order(connection, case.order_id)
            if order.customer_id != case.customer_id:
                raise ConflictError("Order does not belong to the selected customer")
            control_row = connection.execute(
                "SELECT * FROM investigation_control WHERE singleton = 1",
            ).fetchone()
            if control_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            control = SQLiteInvestigationControlRepository._control(control_row)
            connection.execute(
                """INSERT INTO support_cases
                   (case_id, customer_id, order_id, subject, description, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(case.id), str(case.customer_id), str(case.order_id), case.subject, case.description,
                 case.status.value, case.created_at.isoformat(), case.updated_at.isoformat()),
            )
            job = InvestigationJob(
                id=uuid4(), case_id=case.id, customer_id=case.customer_id,
                owner_user_id=owner_user_id, idempotency_key=idempotency_key,
                request_fingerprint=fingerprint, status=InvestigationJobStatus.PENDING,
                created_at=case.created_at, updated_at=case.created_at,
                submission_mode=control.mode, mode_version=control.version,
            )
            connection.execute(
                """INSERT INTO investigation_jobs
                   (job_id, case_id, customer_id, owner_user_id, idempotency_key,
                    request_fingerprint, status, claim_id, claimed_by, claimed_at,
                    lease_expires_at, recovery_reason, created_at, updated_at,
                    submission_mode, mode_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(job.id), str(job.case_id), str(job.customer_id), str(job.owner_user_id),
                 str(job.idempotency_key), job.request_fingerprint, job.status.value,
                 None, None, None, None, None, job.created_at.isoformat(),
                 job.updated_at.isoformat(), job.submission_mode.value, job.mode_version),
            )
            return case, job, True

    def register_manual_start(self, job_id: UUID, *, admin_user_id: UUID,
                              idempotency_key: UUID,
                              now: datetime | None = None) -> tuple[InvestigationJob, bool]:
        """Persist one ADMIN start request; claiming remains the execution gate."""
        timestamp = self._claim_time(now)
        fingerprint = sha256(json.dumps(
            {"job_id": str(job_id)}, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with self._persistence.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM investigation_start_requests
                   WHERE admin_user_id = ? AND idempotency_key = ?""",
                (str(admin_user_id), str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint or existing["job_id"] != str(job_id):
                    raise ConflictError("Idempotency key conflicts with an investigation start request")
                row = connection.execute(
                    "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
                ).fetchone()
                if row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                return self._job(row), False

            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.status is not InvestigationJobStatus.PENDING:
                raise ConflictError("Investigation has already started")
            if connection.execute(
                "SELECT 1 FROM workflow_runs WHERE case_id = ? LIMIT 1", (str(job.case_id),),
            ).fetchone() is not None:
                raise ConflictError("Case already has a workflow investigation")
            prior = connection.execute(
                "SELECT 1 FROM investigation_start_requests WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if prior is not None:
                raise ConflictError("Investigation start has already been requested")
            connection.execute(
                """INSERT INTO investigation_start_requests
                   (admin_user_id, idempotency_key, job_id, request_fingerprint, requested_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(admin_user_id), str(idempotency_key), str(job_id), fingerprint,
                 timestamp.isoformat()),
            )
            return job, True

    def get_by_case(self, case_id: UUID) -> InvestigationJob:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE case_id = ?", (str(case_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            return self._job(row)

    def get(self, job_id: UUID) -> InvestigationJob:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            return self._job(row)

    def claim(self, job_id: UUID, *, worker_id: UUID, lease_seconds: int = 60,
              now: datetime | None = None) -> InvestigationJobClaim | None:
        """Lease one known pending job without selecting or replaying another job.

        The automatic dispatcher uses this targeted variant so a new complaint
        can never cause an older pending job to run incidentally.  A job that
        has already been claimed (including after a crash) is deliberately not
        reclaimed here.
        """
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        claimed_at = self._claim_time(now)
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self._persistence.transaction() as connection:
            select = "SELECT * FROM investigation_jobs WHERE job_id = ?"
            if getattr(self._persistence, "supports_skip_locked", False):
                select += " FOR UPDATE"
            row = connection.execute(select, (str(job_id),)).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.status is not InvestigationJobStatus.PENDING:
                return None
            claim_id = uuid4()
            updated = connection.execute(
                """UPDATE investigation_jobs
                   SET status = ?, claim_id = ?, claimed_by = ?, claimed_at = ?,
                       lease_expires_at = ?, recovery_reason = NULL, updated_at = ?
                   WHERE job_id = ? AND status = ?""",
                (InvestigationJobStatus.CLAIMED.value, str(claim_id), str(worker_id),
                 claimed_at.isoformat(), lease_expires_at.isoformat(), claimed_at.isoformat(),
                 str(job_id), InvestigationJobStatus.PENDING.value),
            )
            if updated.rowcount != 1:
                return None
            claimed_row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if claimed_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            claimed = self._job(claimed_row)
            return InvestigationJobClaim(
                job=claimed, claim_id=claim_id, execution=self._execution(claimed),
            )

    def claim_next(self, *, worker_id: UUID, lease_seconds: int = 60,
                   now: datetime | None = None) -> InvestigationJobClaim | None:
        """Atomically lease the oldest pending job without starting any work.

        A stale claim is made visible as recovery-required before selection. It
        is never returned to PENDING, so a restart cannot silently replay it.
        """
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        claimed_at = self._claim_time(now)
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        with self._persistence.transaction() as connection:
            expired_select = """SELECT * FROM investigation_jobs
                   WHERE status = ? AND lease_expires_at <= ?"""
            if getattr(self._persistence, "supports_skip_locked", False):
                expired_select += " FOR UPDATE SKIP LOCKED"
            expired_rows = connection.execute(
                expired_select, (InvestigationJobStatus.CLAIMED.value, claimed_at.isoformat()),
            ).fetchall()
            for expired_row in expired_rows:
                expired_job = self._job(expired_row)
                self._require_bound_workflow_recovery(
                    connection, job=expired_job, observed_at=claimed_at,
                )
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.LEASE_EXPIRED.value, claimed_at.isoformat(),
                     str(expired_job.id), InvestigationJobStatus.CLAIMED.value),
                )
            pending_select = """SELECT * FROM investigation_jobs
                   WHERE status = ?
                   ORDER BY created_at, job_id
                   LIMIT 1"""
            if getattr(self._persistence, "supports_skip_locked", False):
                pending_select += " FOR UPDATE SKIP LOCKED"
            row = connection.execute(
                pending_select, (InvestigationJobStatus.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            job_id = UUID(row["job_id"])
            claim_id = uuid4()
            updated = connection.execute(
                """UPDATE investigation_jobs
                   SET status = ?, claim_id = ?, claimed_by = ?, claimed_at = ?,
                       lease_expires_at = ?, recovery_reason = NULL, updated_at = ?
                   WHERE job_id = ? AND status = ?""",
                (InvestigationJobStatus.CLAIMED.value, str(claim_id), str(worker_id),
                 claimed_at.isoformat(), lease_expires_at.isoformat(), claimed_at.isoformat(),
                 str(job_id), InvestigationJobStatus.PENDING.value),
            )
            if updated.rowcount != 1:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            claimed_row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if claimed_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            job = self._job(claimed_row)
            return InvestigationJobClaim(job=job, claim_id=claim_id, execution=self._execution(job))

    def require_recovery(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                         now: datetime | None = None) -> InvestigationJob:
        """Record a worker-reported ambiguous claim; it has no retry path here."""
        timestamp = self._claim_time(now)
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if (job.status != InvestigationJobStatus.CLAIMED or job.claim_id != claim_id
                    or job.claimed_by != worker_id):
                raise ConflictError("Investigation job claim is no longer active")
            # A durable attempt marker can outlive a process crash.  Explicit
            # recovery must make its already-reserved workflow non-runnable as
            # well; it must never be treated as an invitation to retry.
            self._require_bound_workflow_recovery(
                connection, job=job, observed_at=timestamp,
            )
            updated = connection.execute(
                """UPDATE investigation_jobs
                   SET status = ?, recovery_reason = ?, updated_at = ?
                   WHERE job_id = ? AND status = ? AND claim_id = ? AND claimed_by = ?""",
                (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                 InvestigationRecoveryReason.CLAIM_AMBIGUOUS.value, timestamp.isoformat(),
                 str(job_id), InvestigationJobStatus.CLAIMED.value, str(claim_id), str(worker_id)),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation job claim is no longer active")
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return self._job(row)

    def bind_claim(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                   provider: Literal["local_scripted", "bedrock"],
                   now: datetime | None = None) -> WorkflowReservationResult:
        """Bind one active claim to its one deterministic workflow reservation.

        This creates no runtime, provider, checkpoint, or graph invocation. An
        expired, mismatched, or colliding claim is deliberately left for manual
        recovery rather than being rebound under a new execution identity.
        """
        observed_at = self._claim_time(now)
        expired = False
        collision = False
        incompatible = False
        result: WorkflowReservationResult | None = None
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.claim_id != claim_id or job.claimed_by != worker_id:
                raise ConflictError("Investigation job claim is not owned by this worker")
            if job.status != InvestigationJobStatus.CLAIMED:
                raise ConflictError("Investigation job is not claimable for workflow binding")
            if job.lease_expires_at is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            case_row = connection.execute(
                """SELECT customer_id, subject, description FROM support_cases
                   WHERE case_id = ? AND customer_id = ?""",
                (str(job.case_id), str(job.customer_id)),
            ).fetchone()
            if case_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            if job.lease_expires_at <= observed_at:
                self._require_bound_workflow_recovery(
                    connection, job=job, observed_at=observed_at,
                )
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ? AND claim_id = ? AND claimed_by = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.LEASE_EXPIRED.value, observed_at.isoformat(),
                     str(job.id), InvestigationJobStatus.CLAIMED.value,
                     str(claim_id), str(worker_id)),
                )
                expired = True
            elif connection.execute(
                """SELECT 1 FROM workflow_runs
                   WHERE case_id = ? AND workflow_id <> ? LIMIT 1""",
                (str(job.case_id), str(job.id)),
            ).fetchone() is not None:
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ? AND claim_id = ? AND claimed_by = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.CLAIM_AMBIGUOUS.value, observed_at.isoformat(),
                     str(job.id), InvestigationJobStatus.CLAIMED.value,
                     str(claim_id), str(worker_id)),
                )
                collision = True
            elif job.workflow_id is not None:
                if job.workflow_id != job.id:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                run_row = connection.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(job.workflow_id),),
                ).fetchone()
                if run_row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                run = SQLiteWorkflowMetadataRepository._run(run_row)
                if (run.run_id != job.id or run.request_id != job.id
                        or run.case_id != job.case_id or run.owner_user_id != worker_id):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                try:
                    self._stored_request(job, run)
                except ValueError:
                    self._require_claim_recovery(
                        connection, job=job, observed_at=observed_at,
                    )
                    incompatible = True
                else:
                    if run.provider != provider:
                        raise ConflictError("Investigation job reservation provider is immutable")
                    result = WorkflowReservationResult(run=run, created=False)
            else:
                if connection.execute(
                    "SELECT 1 FROM workflow_runs WHERE workflow_id = ? OR run_id = ?",
                    (str(job.id), str(job.id)),
                ).fetchone() is not None:
                    connection.execute(
                        """UPDATE investigation_jobs
                           SET status = ?, recovery_reason = ?, updated_at = ?
                           WHERE job_id = ? AND status = ? AND workflow_id IS NULL""",
                        (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                         InvestigationRecoveryReason.CLAIM_AMBIGUOUS.value, observed_at.isoformat(),
                         str(job.id), InvestigationJobStatus.CLAIMED.value),
                    )
                    collision = True
                else:
                    bound_at = observed_at
                    if bound_at <= job.updated_at:
                        bound_at = job.updated_at + timedelta(microseconds=1)
                    _, descriptor, descriptor_fingerprint = self._canonical_request(job, case_row)
                    reservation = WorkflowRunReservation(
                        workflow_id=job.id, run_id=job.id, request_id=job.id,
                        case_id=job.case_id, owner_user_id=worker_id,
                        request_fingerprint=descriptor_fingerprint, provider=provider,
                    )
                    run = WorkflowRunMetadata(
                        **reservation.model_dump(), status=WorkflowMetadataStatus.RUNNING,
                        created_at=bound_at, updated_at=bound_at,
                    )
                    connection.execute(
                        """INSERT INTO workflow_runs
                           (workflow_id, run_id, case_id, owner_user_id, request_id,
                            request_fingerprint, provider, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(run.workflow_id), str(run.run_id), str(run.case_id),
                         str(run.owner_user_id), str(run.request_id), run.request_fingerprint,
                         run.provider, run.status.value, run.created_at.isoformat(),
                         run.updated_at.isoformat()),
                    )
                    SQLiteWorkflowMetadataRepository._append_run_event(
                        connection, run=run, kind="CREATED", previous_status=None,
                    )
                    updated = connection.execute(
                        """UPDATE investigation_jobs
                           SET workflow_id = ?, bound_at = ?, agent_request_json = ?,
                               agent_request_fingerprint = ?, updated_at = ?
                           WHERE job_id = ? AND status = ? AND claim_id = ?
                             AND claimed_by = ? AND workflow_id IS NULL""",
                        (str(run.workflow_id), bound_at.isoformat(), descriptor,
                         descriptor_fingerprint, bound_at.isoformat(),
                         str(job.id), InvestigationJobStatus.CLAIMED.value,
                         str(claim_id), str(worker_id)),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError("Investigation job claim is no longer active")
                    result = WorkflowReservationResult(run=run, created=True)
        if expired:
            raise ConflictError("Investigation job lease expired and requires recovery")
        if collision:
            raise ConflictError("Investigation job binding requires recovery")
        if incompatible:
            raise ConflictError("Investigation job reservation requires recovery")
        if result is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return result

    def mark_execution_attempt(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                               now: datetime | None = None) -> InvestigationExecutionAttempt:
        """Durably authorize the first future execution attempt, before invocation.

        No graph, provider, checkpoint, or background task is started here.
        Once committed, this marker intentionally blocks every later attempt
        until an explicit recovery workflow exists.
        """
        observed_at = self._claim_time(now)
        expired = False
        incompatible = False
        attempt: InvestigationExecutionAttempt | None = None
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.claim_id != claim_id or job.claimed_by != worker_id:
                raise ConflictError("Investigation job claim is not owned by this worker")
            if job.status != InvestigationJobStatus.CLAIMED or job.workflow_id is None:
                raise ConflictError("Investigation job is not ready for execution")
            if job.execution_attempt_id is not None:
                raise ConflictError("Investigation job already has an execution attempt")
            if job.lease_expires_at is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            if job.lease_expires_at <= observed_at:
                self._require_bound_workflow_recovery(
                    connection, job=job, observed_at=observed_at,
                )
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.LEASE_EXPIRED.value, observed_at.isoformat(),
                     str(job.id), InvestigationJobStatus.CLAIMED.value),
                )
                expired = True
            else:
                run_row = connection.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(job.workflow_id),),
                ).fetchone()
                if run_row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                run = SQLiteWorkflowMetadataRepository._run(run_row)
                if (run.workflow_id != job.id or run.run_id != job.id or run.request_id != job.id
                        or run.case_id != job.case_id or run.owner_user_id != worker_id):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                try:
                    self._stored_request(job, run)
                except ValueError:
                    self._require_claim_recovery(
                        connection, job=job, observed_at=observed_at,
                    )
                    incompatible = True
                else:
                    attempted_at = observed_at
                    if attempted_at <= job.updated_at:
                        attempted_at = job.updated_at + timedelta(microseconds=1)
                    attempt_id = uuid4()
                    updated = connection.execute(
                        """UPDATE investigation_jobs
                           SET execution_attempt_id = ?, execution_attempted_at = ?, updated_at = ?
                           WHERE job_id = ? AND status = ? AND claim_id = ?
                             AND claimed_by = ? AND execution_attempt_id IS NULL""",
                        (str(attempt_id), attempted_at.isoformat(), attempted_at.isoformat(),
                         str(job.id), InvestigationJobStatus.CLAIMED.value,
                         str(claim_id), str(worker_id)),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError("Investigation job execution attempt is no longer available")
                    attempt = InvestigationExecutionAttempt(
                        job_id=job.id, workflow_id=job.workflow_id, attempt_id=attempt_id,
                        attempted_at=attempted_at, agent_request_json=job.agent_request_json or "",
                        agent_request_fingerprint=job.agent_request_fingerprint or "",
                    )
        if expired:
            raise ConflictError("Investigation job lease expired and requires recovery")
        if incompatible:
            raise ConflictError("Investigation job reservation requires recovery")
        if attempt is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return attempt

    @contextmanager
    def execution_context(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                          attempt_id: UUID, now: datetime | None = None) -> Iterator[InvestigationExecutionContext]:
        """Enter the sole server-side tool scope for one durable attempt.

        It accepts only opaque persisted identifiers.  Customer, case, order,
        workflow, worker, and request data are re-read from SQLite and never
        taken from a caller payload.  It starts no graph or provider.
        """
        observed_at = self._claim_time(now)
        expired = False
        incompatible = False
        context: InvestigationExecutionContext | None = None
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.claim_id != claim_id or job.claimed_by != worker_id:
                raise ConflictError("Investigation job claim is not owned by this worker")
            if (job.status != InvestigationJobStatus.CLAIMED or job.workflow_id is None
                    or job.execution_attempt_id != attempt_id):
                raise ConflictError("Investigation job has no matching execution attempt")
            if job.lease_expires_at is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            if job.lease_expires_at <= observed_at:
                self._require_bound_workflow_recovery(
                    connection, job=job, observed_at=observed_at,
                )
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.LEASE_EXPIRED.value, observed_at.isoformat(),
                     str(job.id), InvestigationJobStatus.CLAIMED.value),
                )
                expired = True
            else:
                run_row = connection.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(job.workflow_id),),
                ).fetchone()
                case_row = connection.execute(
                    """SELECT c.case_id, c.customer_id, c.order_id, o.customer_id AS order_customer_id
                       FROM support_cases c JOIN orders o ON o.order_id = c.order_id
                       WHERE c.case_id = ?""",
                    (str(job.case_id),),
                ).fetchone()
                if run_row is None or case_row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                run = SQLiteWorkflowMetadataRepository._run(run_row)
                if (run.workflow_id != job.id or run.run_id != job.id or run.request_id != job.id
                        or run.case_id != job.case_id or run.owner_user_id != worker_id
                        or case_row["customer_id"] != str(job.customer_id)
                        or case_row["order_customer_id"] != str(job.customer_id)):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                try:
                    self._stored_request(job, run)
                except ValueError:
                    self._require_claim_recovery(
                        connection, job=job, observed_at=observed_at,
                    )
                    incompatible = True
                else:
                    context = InvestigationExecutionContext(
                        job_id=job.id, execution_id=job.id,
                        execution_attempt_id=job.execution_attempt_id,
                        workflow_id=job.workflow_id, worker_id=job.claimed_by,
                        customer_id=job.customer_id, case_id=job.case_id,
                        order_id=UUID(case_row["order_id"]),
                        agent_request_json=job.agent_request_json or "",
                        agent_request_fingerprint=job.agent_request_fingerprint or "",
                    )
        if expired:
            raise ConflictError("Investigation job lease expired and requires recovery")
        if incompatible:
            raise ConflictError("Investigation job reservation requires recovery")
        if context is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        with trusted_investigation_execution(context):
            yield context

    def execution_request(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                          now: datetime | None = None) -> AgentRequest:
        """Return the exact bound descriptor for a future trusted executor.

        This is a validation/read gate only. It deliberately does not create a
        workflow, call a provider, or record an execution attempt.
        """
        observed_at = self._claim_time(now)
        expired = False
        incompatible = False
        request: AgentRequest | None = None
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ?", (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = self._job(row)
            if job.claim_id != claim_id or job.claimed_by != worker_id:
                raise ConflictError("Investigation job claim is not owned by this worker")
            if job.status != InvestigationJobStatus.CLAIMED or job.workflow_id is None:
                raise ConflictError("Investigation job is not ready for execution")
            if job.execution_attempt_id is not None:
                raise ConflictError("Investigation job already has an execution attempt")
            if job.lease_expires_at is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            if job.lease_expires_at <= observed_at:
                self._require_bound_workflow_recovery(
                    connection, job=job, observed_at=observed_at,
                )
                connection.execute(
                    """UPDATE investigation_jobs
                       SET status = ?, recovery_reason = ?, updated_at = ?
                       WHERE job_id = ? AND status = ?""",
                    (InvestigationJobStatus.RECOVERY_REQUIRED.value,
                     InvestigationRecoveryReason.LEASE_EXPIRED.value, observed_at.isoformat(),
                     str(job.id), InvestigationJobStatus.CLAIMED.value),
                )
                expired = True
            else:
                run_row = connection.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(job.workflow_id),),
                ).fetchone()
                if run_row is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                run = SQLiteWorkflowMetadataRepository._run(run_row)
                if (run.workflow_id != job.id or run.run_id != job.id or run.request_id != job.id
                        or run.case_id != job.case_id or run.owner_user_id != worker_id):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
                try:
                    request = self._stored_request(job, run)
                except ValueError:
                    self._require_claim_recovery(
                        connection, job=job, observed_at=observed_at,
                    )
                    incompatible = True
        if expired:
            raise ConflictError("Investigation job lease expired and requires recovery")
        if incompatible:
            raise ConflictError("Investigation job reservation requires recovery")
        if request is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return request


class SQLiteRefundEvidenceRepository:
    """Opt-in durable trusted-delivery and refund-evidence repository."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _delivery_fact(row: sqlite3.Row) -> AuthoritativeDeliveryFact:
        try:
            return AuthoritativeDeliveryFact.model_validate({
                "id": row["delivery_fact_id"], "case_id": row["case_id"],
                "order_id": row["order_id"], "customer_id": row["customer_id"],
                "sku": row["sku"], "quantity": row["quantity"],
                "delivered_at": row["delivered_at"], "reason": row["reason"],
                "condition": row["item_condition"], "source_id": row["source_id"],
                "source_reference": row["source_reference"], "verified_at": row["verified_at"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @classmethod
    def _evidence(cls, row: sqlite3.Row) -> EligibilityEvidence:
        try:
            result = EligibilityResult.model_validate(json.loads(row["assessment_json"]))
            evidence = EligibilityEvidence.model_validate({
                "id": row["evidence_id"], "case_id": row["case_id"],
                "order_id": row["order_id"], "customer_id": row["customer_id"],
                "sku": row["sku"], "delivery_fact_id": row["delivery_fact_id"],
                "delivery_source_id": row["source_id"],
                "delivery_source_reference": row["source_reference"],
                "result": result, "created_at": row["created_at"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
        try:
            cls._validate_evidence_shape(evidence)
        except ConflictError:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
        if evidence.result.policy_id != row["policy_id"] or evidence.result.policy_version != row["policy_version"]:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return evidence

    @staticmethod
    def _validate_evidence_shape(evidence: EligibilityEvidence) -> None:
        result = evidence.result
        if (
            result.evidence_id != evidence.id
            or result.case_id != evidence.case_id
            or result.order_id != evidence.order_id
            or result.customer_id != evidence.customer_id
            or result.sku != evidence.sku
            or result.assessment_basis != "trusted_delivery_record"
            or result.return_eligible != result.refund_eligible
            or result.refund_eligible != (not result.denial_reasons)
            or evidence.created_at.tzinfo is None
        ):
            raise ConflictError("Eligibility evidence is invalid or forged")

    @staticmethod
    def _require_fact_bindings(connection: sqlite3.Connection,
                               fact: AuthoritativeDeliveryFact) -> None:
        row = connection.execute(
            """SELECT c.customer_id AS case_customer_id, c.order_id AS case_order_id,
                      o.customer_id AS order_customer_id, i.quantity AS purchased_quantity
               FROM support_cases c
               JOIN orders o ON o.order_id = c.order_id
               JOIN order_items i ON i.order_id = o.order_id AND i.sku = ?
               WHERE c.case_id = ?""",
            (fact.sku, str(fact.case_id)),
        ).fetchone()
        if (row is None or row["case_order_id"] != str(fact.order_id)
                or row["case_customer_id"] != str(fact.customer_id)
                or row["order_customer_id"] != str(fact.customer_id)
                or fact.quantity > row["purchased_quantity"]):
            raise ConflictError("Delivery fact does not match the current case, order, customer, and item")

    @classmethod
    def _require_evidence_bindings(cls, connection: sqlite3.Connection,
                                   evidence: EligibilityEvidence) -> None:
        fact_row = connection.execute(
            "SELECT * FROM delivery_facts WHERE delivery_fact_id = ?",
            (str(evidence.delivery_fact_id),),
        ).fetchone()
        if fact_row is None:
            raise ConflictError("Eligibility evidence delivery fact is missing")
        fact = cls._delivery_fact(fact_row)
        cls._require_fact_bindings(connection, fact)
        result = evidence.result
        if (
            fact.case_id != evidence.case_id or fact.order_id != evidence.order_id
            or fact.customer_id != evidence.customer_id or fact.sku != evidence.sku
            or fact.source_id != evidence.delivery_source_id
            or fact.source_reference != evidence.delivery_source_reference
            or result.requested_quantity != fact.quantity
            or result.reason != fact.reason or result.condition != fact.condition
            or evidence.created_at < fact.verified_at
        ):
            raise ConflictError("Eligibility evidence does not match its trusted delivery fact")
        order_item = connection.execute(
            "SELECT quantity FROM order_items WHERE order_id = ? AND sku = ?",
            (str(evidence.order_id), evidence.sku),
        ).fetchone()
        if order_item is None or result.purchased_quantity != order_item["quantity"]:
            raise ConflictError("Eligibility evidence does not match its purchased item")
        expected_days = (
            evidence.created_at.astimezone(timezone.utc).date()
            - fact.delivered_at.astimezone(timezone.utc).date()
        ).days
        if result.days_since_delivery != expected_days:
            raise ConflictError("Eligibility evidence delivery age is invalid")

    def add_trusted_source(self, source: TrustedDeliverySource) -> None:
        source = TrustedDeliverySource.model_validate(source.model_dump())
        with self._persistence.transaction() as connection:
            existing = connection.execute(
                "SELECT source_id FROM trusted_delivery_sources WHERE source_id = ?", (source.id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO trusted_delivery_sources(source_id) VALUES (?)", (source.id,),
                )

    def store_delivery_fact(self, fact: AuthoritativeDeliveryFact) -> AuthoritativeDeliveryFact:
        fact = AuthoritativeDeliveryFact.model_validate(fact.model_dump())
        with self._persistence.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM trusted_delivery_sources WHERE source_id = ?", (fact.source_id,),
            ).fetchone() is None:
                raise ConflictError("Trusted delivery source is not persisted")
            self._require_fact_bindings(connection, fact)

            candidates = (
                ("SELECT * FROM delivery_facts WHERE delivery_fact_id = ?", (str(fact.id),),
                 "Delivery fact ID conflicts with an existing record"),
                ("SELECT * FROM delivery_facts WHERE source_id = ? AND source_reference = ?",
                 (fact.source_id, fact.source_reference),
                 "Delivery fact provenance conflicts with an existing record"),
                ("SELECT * FROM delivery_facts WHERE case_id = ? AND sku = ?",
                 (str(fact.case_id), fact.sku),
                 "Conflicting delivery fact already exists for this case item"),
            )
            for query, parameters, conflict_message in candidates:
                row = connection.execute(query, parameters).fetchone()
                if row is not None:
                    existing = self._delivery_fact(row)
                    if existing == fact:
                        return existing
                    raise ConflictError(conflict_message)

            connection.execute(
                """INSERT INTO delivery_facts
                   (delivery_fact_id, case_id, order_id, customer_id, sku, quantity,
                    delivered_at, reason, item_condition, source_id, source_reference, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(fact.id), str(fact.case_id), str(fact.order_id), str(fact.customer_id),
                 fact.sku, fact.quantity, fact.delivered_at.isoformat(), fact.reason.value,
                 fact.condition.value, fact.source_id, fact.source_reference,
                 fact.verified_at.isoformat()),
            )
        return fact

    def get_delivery_fact(self, case_id: UUID, sku: str) -> AuthoritativeDeliveryFact | None:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_facts WHERE case_id = ? AND sku = ?",
                (str(case_id), sku),
            ).fetchone()
            if row is None:
                return None
            fact = self._delivery_fact(row)
            try:
                self._require_fact_bindings(connection, fact)
            except ConflictError:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
            return fact

    def store_eligibility_evidence(self, evidence: EligibilityEvidence) -> EligibilityEvidence:
        evidence = EligibilityEvidence.model_validate(evidence.model_dump())
        self._validate_evidence_shape(evidence)
        with self._persistence.transaction() as connection:
            result = evidence.result
            self._require_evidence_bindings(connection, evidence)

            existing_row = connection.execute(
                """SELECT e.*, f.source_id, f.source_reference
                   FROM eligibility_evidence e
                   JOIN delivery_facts f ON f.delivery_fact_id = e.delivery_fact_id
                   WHERE e.evidence_id = ?""",
                (str(evidence.id),),
            ).fetchone()
            if existing_row is not None:
                existing = self._evidence(existing_row)
                if existing == evidence:
                    return existing
                raise ConflictError("Eligibility evidence ID conflicts with an existing record")

            assessment_json = json.dumps(
                result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            )
            connection.execute(
                """INSERT INTO eligibility_evidence
                   (evidence_id, case_id, order_id, customer_id, sku, delivery_fact_id,
                    policy_id, policy_version, assessment_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(evidence.id), str(evidence.case_id), str(evidence.order_id),
                 str(evidence.customer_id), evidence.sku, str(evidence.delivery_fact_id),
                 result.policy_id, result.policy_version, assessment_json,
                 evidence.created_at.isoformat()),
            )
        return evidence

    def get_eligibility_evidence(self, evidence_id: UUID) -> EligibilityEvidence:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                """SELECT e.*, f.source_id, f.source_reference
                   FROM eligibility_evidence e
                   JOIN delivery_facts f ON f.delivery_fact_id = e.delivery_fact_id
                   WHERE e.evidence_id = ?""",
                (str(evidence_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Eligibility evidence not found")
            evidence = self._evidence(row)
            try:
                self._require_evidence_bindings(connection, evidence)
            except ConflictError:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
            return evidence


class SQLiteProposalRepository:
    """Opt-in proposal persistence with atomic, append-only review decisions."""

    _ANCHOR_OWNER = "proposal-review-persistence"

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _proposal(row: sqlite3.Row) -> ResolutionProposal:
        try:
            return ResolutionProposal.model_validate({
                "id": row["proposal_id"], "case_id": row["case_id"],
                "action": row["action"], "rationale": row["rationale"],
                "eligibility_evidence_id": row["eligibility_evidence_id"],
                "status": row["status"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "reviewed_at": row["reviewed_at"],
                "reviewer_name": row["reviewer_user_id"], "review_note": row["review_note"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _binding(row: sqlite3.Row) -> WorkflowReviewBinding:
        try:
            return WorkflowReviewBinding.model_validate({
                "workflow_id": row["workflow_id"], "case_id": row["case_id"],
                "review_id": row["review_id"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @classmethod
    def _ensure_review_anchor(cls, connection: sqlite3.Connection, *,
                              workflow_id: UUID, case_id: UUID,
                              request_id: UUID, proposal_id: UUID,
                              created_at: datetime) -> datetime:
        row = connection.execute(
            """SELECT workflow_id, case_id, status, updated_at
               FROM workflow_runs WHERE workflow_id = ?""",
            (str(workflow_id),),
        ).fetchone()
        if row is not None:
            if row["case_id"] != str(case_id) or row["status"] != "REVIEW_REQUIRED":
                raise ConflictError("Workflow cannot accept this proposal review binding")
            try:
                updated_at = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            except Exception:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
            if updated_at.tzinfo is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return updated_at
        timestamp = created_at.isoformat()
        connection.execute(
            """INSERT INTO workflow_runs
               (workflow_id, run_id, case_id, owner_user_id, request_id,
                request_fingerprint, provider, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'local_scripted', 'REVIEW_REQUIRED', ?, ?)""",
            (str(workflow_id), str(workflow_id), str(case_id), cls._ANCHOR_OWNER,
             str(request_id), f"proposal-review:{proposal_id}", timestamp, timestamp),
        )
        return created_at

    @staticmethod
    def _next_sequence(connection: sqlite3.Connection, workflow_id: UUID) -> int:
        row = connection.execute(
            "SELECT coalesce(max(sequence), 0) + 1 FROM workflow_audit_events WHERE workflow_id = ?",
            (str(workflow_id),),
        ).fetchone()
        return row[0]

    @classmethod
    def _append_audit(cls, connection: sqlite3.Connection, *, workflow_id: UUID,
                      kind: str, proposal: ResolutionProposal,
                      review_id: UUID | None, created_at: datetime) -> None:
        payload = {
            "kind": kind, "workflow_id": str(workflow_id),
            "case_id": str(proposal.case_id), "proposal_id": str(proposal.id),
            "review_id": str(review_id) if review_id is not None else None,
            "proposal_status": proposal.status.value,
            "reviewer_user_id": proposal.reviewer_name,
            "review_note": proposal.review_note,
        }
        connection.execute(
            """INSERT INTO workflow_audit_events
               (workflow_id, sequence, kind, proposal_id, reviewer_user_id,
                proposal_status, event_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(workflow_id), cls._next_sequence(connection, workflow_id), kind,
             str(proposal.id), proposal.reviewer_name, proposal.status.value,
             json.dumps(payload, sort_keys=True, separators=(",", ":")),
             created_at.isoformat()),
        )

    def create(self, proposal: ResolutionProposal) -> ResolutionProposal:
        proposal = ResolutionProposal.model_validate(proposal.model_dump())
        with self._persistence.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM resolution_proposals WHERE proposal_id = ?", (str(proposal.id),),
            ).fetchone() is not None:
                raise ConflictError("Proposal already exists")
            connection.execute(
                """INSERT INTO resolution_proposals
                   (proposal_id, case_id, action, rationale, eligibility_evidence_id,
                    status, created_at, updated_at, reviewed_at, reviewer_user_id, review_note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(proposal.id), str(proposal.case_id), proposal.action.value,
                 proposal.rationale,
                 str(proposal.eligibility_evidence_id) if proposal.eligibility_evidence_id else None,
                 proposal.status.value, proposal.created_at.isoformat(),
                 proposal.updated_at.isoformat(), None, None, None),
            )
        return proposal

    def get(self, proposal_id: UUID) -> ResolutionProposal:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_proposals WHERE proposal_id = ?", (str(proposal_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Proposal not found")
            return self._proposal(row)

    def list_for_case(self, case_id: UUID) -> list[ResolutionProposal]:
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM resolution_proposals
                   WHERE case_id = ? ORDER BY created_at, proposal_id""",
                (str(case_id),),
            ).fetchall()
            return [self._proposal(row) for row in rows]

    def bind_workflow_review(self, proposal_id: UUID,
                             binding: WorkflowReviewBinding) -> WorkflowReviewBinding:
        binding = WorkflowReviewBinding.model_validate(binding.model_dump())
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_proposals WHERE proposal_id = ?", (str(proposal_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Proposal not found")
            proposal = self._proposal(row)
            if (proposal.case_id != binding.case_id
                    or proposal.status != ProposalStatus.PENDING_REVIEW):
                raise ConflictError("Proposal cannot be bound to this workflow review")

            existing_row = connection.execute(
                "SELECT * FROM workflow_review_bindings WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
            if existing_row is not None:
                existing = self._binding(existing_row)
                if existing == binding:
                    return existing
                raise ConflictError("Proposal is already bound to a different workflow review")
            if connection.execute(
                """SELECT 1 FROM workflow_review_bindings
                   WHERE workflow_id = ? OR review_id = ?""",
                (str(binding.workflow_id), str(binding.review_id)),
            ).fetchone() is not None:
                raise ConflictError("Workflow review binding is already in use")

            now = datetime.now(timezone.utc)
            self._ensure_review_anchor(
                connection, workflow_id=binding.workflow_id, case_id=binding.case_id,
                request_id=binding.review_id, proposal_id=proposal.id, created_at=now,
            )
            connection.execute(
                """INSERT INTO workflow_review_bindings
                   (proposal_id, workflow_id, case_id, review_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(proposal.id), str(binding.workflow_id), str(binding.case_id),
                 str(binding.review_id), now.isoformat()),
            )
            self._append_audit(
                connection, workflow_id=binding.workflow_id, kind="REVIEW_REQUIRED",
                proposal=proposal, review_id=binding.review_id, created_at=now,
            )
            return binding

    def matches_workflow_review(self, proposal_id: UUID,
                                binding: WorkflowReviewBinding) -> bool:
        binding = WorkflowReviewBinding.model_validate(binding.model_dump())
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_review_bindings WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
            return row is not None and self._binding(row) == binding

    def review(self, proposal_id: UUID, decision: ProposalStatus,
               review: HumanReview, *, workflow_id: UUID | None,
               review_id: UUID | None) -> ResolutionProposal:
        if decision not in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
            raise ConflictError("Review decision must be APPROVED or REJECTED")
        review = HumanReview.model_validate(review.model_dump())
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_proposals WHERE proposal_id = ?", (str(proposal_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Proposal not found")
            proposal = self._proposal(row)
            if proposal.status != ProposalStatus.PENDING_REVIEW:
                raise ConflictError("Proposal has already been reviewed")

            binding_row = connection.execute(
                "SELECT * FROM workflow_review_bindings WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
            if binding_row is not None:
                binding = self._binding(binding_row)
                if (workflow_id != binding.workflow_id or review_id != binding.review_id
                        or proposal.case_id != binding.case_id):
                    raise ConflictError(
                        "Workflow-bound proposal must be reviewed through its active workflow"
                    )
                anchor = connection.execute(
                    """SELECT case_id, owner_user_id, status, updated_at
                       FROM workflow_runs WHERE workflow_id = ?""",
                    (str(binding.workflow_id),),
                ).fetchone()
                if (anchor is None or anchor["case_id"] != str(binding.case_id)
                        or anchor["status"] not in ("REVIEW_REQUIRED", "RESUMING")):
                    raise ConflictError("Workflow review binding is stale or invalid")
                audit_workflow_id = binding.workflow_id
                audit_review_id = binding.review_id
                try:
                    anchor_updated_at = datetime.fromisoformat(
                        anchor["updated_at"].replace("Z", "+00:00")
                    )
                except Exception:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
                if anchor_updated_at.tzinfo is None:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            else:
                if workflow_id is not None or review_id is not None:
                    raise ConflictError("Proposal is not bound to the supplied workflow review")
                audit_workflow_id = proposal.id
                audit_review_id = None
                anchor_updated_at = self._ensure_review_anchor(
                    connection, workflow_id=audit_workflow_id, case_id=proposal.case_id,
                    request_id=proposal.id, proposal_id=proposal.id,
                    created_at=datetime.now(timezone.utc),
                )

            now = datetime.now(timezone.utc)
            latest_update = max(proposal.updated_at, anchor_updated_at)
            if now <= latest_update:
                now = latest_update + timedelta(microseconds=1)
            updated = connection.execute(
                """UPDATE resolution_proposals
                   SET status = ?, updated_at = ?, reviewed_at = ?,
                       reviewer_user_id = ?, review_note = ?
                   WHERE proposal_id = ? AND status = 'PENDING_REVIEW'""",
                (decision.value, now.isoformat(), now.isoformat(), review.reviewer_name,
                 review.note, str(proposal.id)),
            )
            if updated.rowcount != 1:
                raise ConflictError("Proposal has already been reviewed")
            reviewed = ResolutionProposal.model_validate({
                **proposal.model_dump(), "status": decision, "updated_at": now,
                "reviewed_at": now, "reviewer_name": review.reviewer_name,
                "review_note": review.note,
            })
            anchor_update = connection.execute(
                """UPDATE workflow_runs SET status = 'REVIEWED', updated_at = ?
                   WHERE workflow_id = ? AND case_id = ?
                     AND status IN ('REVIEW_REQUIRED', 'RESUMING')""",
                (now.isoformat(), str(audit_workflow_id), str(proposal.case_id)),
            )
            if anchor_update.rowcount != 1:
                raise ConflictError("Workflow review binding is stale or invalid")
            if binding_row is not None and anchor["owner_user_id"] != self._ANCHOR_OWNER:
                updated_run = connection.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_id = ?",
                    (str(audit_workflow_id),),
                ).fetchone()
                SQLiteWorkflowMetadataRepository._append_run_event(
                    connection, run=SQLiteWorkflowMetadataRepository._run(updated_run),
                    kind="STATUS_CHANGED", previous_status=WorkflowMetadataStatus(anchor["status"]),
                )
            self._append_audit(
                connection, workflow_id=audit_workflow_id, kind="HUMAN_REVIEW",
                proposal=reviewed, review_id=audit_review_id, created_at=now,
            )
            return reviewed

    def audit_events(self, workflow_id: UUID) -> tuple[dict, ...]:
        """Read immutable repository audit payloads; not a checkpoint recovery API."""
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                """SELECT sequence, kind, proposal_id, reviewer_user_id,
                          proposal_status, event_json
                   FROM workflow_audit_events
                   WHERE workflow_id = ? ORDER BY sequence""",
                (str(workflow_id),),
            ).fetchall()
            try:
                payloads = tuple(json.loads(row["event_json"]) for row in rows)
            except Exception:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
            for expected_sequence, (row, payload) in enumerate(zip(rows, payloads), start=1):
                if (
                    row["sequence"] != expected_sequence
                    or payload.get("kind") != row["kind"]
                    or payload.get("workflow_id") != str(workflow_id)
                    or payload.get("proposal_id") != row["proposal_id"]
                    or payload.get("reviewer_user_id") != row["reviewer_user_id"]
                    or payload.get("proposal_status") != row["proposal_status"]
                ):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return payloads


class SQLiteWorkflowMetadataRepository:
    """Opt-in run metadata and idempotency store; contains no replayable checkpoint."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _run(row: sqlite3.Row) -> WorkflowRunMetadata:
        try:
            run = WorkflowRunMetadata.model_validate({
                "workflow_id": row["workflow_id"], "run_id": row["run_id"],
                "case_id": row["case_id"], "owner_user_id": row["owner_user_id"],
                "request_id": row["request_id"],
                "request_fingerprint": row["request_fingerprint"],
                "provider": row["provider"], "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "failure_category": row["failure_category"],
                "failure_stage": row["failure_stage"],
                "invalid_response_stage": row["invalid_response_stage"],
                "invalid_response_category": row["invalid_response_category"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
        if (run.created_at.tzinfo is None or run.updated_at.tzinfo is None
                or run.updated_at < run.created_at):
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return run

    @staticmethod
    def _event(row: sqlite3.Row) -> WorkflowRunEvent:
        try:
            return WorkflowRunEvent.model_validate({
                "workflow_id": row["workflow_id"], "sequence": row["sequence"],
                "kind": row["kind"], "previous_status": row["previous_status"],
                "status": row["status"], "created_at": row["created_at"],
            })
        except Exception:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _next_run_sequence(connection: sqlite3.Connection, workflow_id: UUID) -> int:
        return connection.execute(
            "SELECT coalesce(max(sequence), 0) + 1 FROM workflow_run_events WHERE workflow_id = ?",
            (str(workflow_id),),
        ).fetchone()[0]

    @classmethod
    def _append_run_event(cls, connection: sqlite3.Connection, *,
                          run: WorkflowRunMetadata, kind: str,
                          previous_status: WorkflowMetadataStatus | None) -> WorkflowRunEvent:
        sequence = cls._next_run_sequence(connection, run.workflow_id)
        event = WorkflowRunEvent(
            workflow_id=run.workflow_id, sequence=sequence, kind=kind,
            previous_status=previous_status, status=run.status,
            created_at=run.updated_at,
        )
        payload = {
            **event.model_dump(mode="json"), "case_id": str(run.case_id),
            "owner_user_id": str(run.owner_user_id), "run_id": str(run.run_id),
        }
        connection.execute(
            """INSERT INTO workflow_run_events
               (workflow_id, sequence, kind, previous_status, status, event_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(event.workflow_id), event.sequence, event.kind,
             event.previous_status.value if event.previous_status else None,
             event.status.value,
             json.dumps(payload, sort_keys=True, separators=(",", ":")),
             event.created_at.isoformat()),
        )
        return event

    def reserve(self, reservation: WorkflowRunReservation) -> WorkflowReservationResult:
        reservation = WorkflowRunReservation.model_validate(reservation.model_dump())
        with self._persistence.transaction() as connection:
            existing_row = connection.execute(
                """SELECT * FROM workflow_runs
                   WHERE owner_user_id = ? AND request_id = ?""",
                (str(reservation.owner_user_id), str(reservation.request_id)),
            ).fetchone()
            if existing_row is not None:
                existing = self._run(existing_row)
                if (existing.request_fingerprint != reservation.request_fingerprint
                        or existing.case_id != reservation.case_id
                        or existing.provider != reservation.provider):
                    raise ConflictError("Request ID conflicts with an existing workflow request")
                return WorkflowReservationResult(run=existing, created=False)
            if connection.execute(
                "SELECT 1 FROM workflow_runs WHERE workflow_id = ? OR run_id = ?",
                (str(reservation.workflow_id), str(reservation.run_id)),
            ).fetchone() is not None:
                raise ConflictError("Workflow or run ID already exists")

            now = datetime.now(timezone.utc)
            run = WorkflowRunMetadata(
                **reservation.model_dump(), status=WorkflowMetadataStatus.RUNNING,
                created_at=now, updated_at=now,
            )
            connection.execute(
                """INSERT INTO workflow_runs
                   (workflow_id, run_id, case_id, owner_user_id, request_id,
                    request_fingerprint, provider, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(run.workflow_id), str(run.run_id), str(run.case_id),
                 str(run.owner_user_id), str(run.request_id), run.request_fingerprint,
                 run.provider, run.status.value, run.created_at.isoformat(),
                 run.updated_at.isoformat()),
            )
            self._append_run_event(
                connection, run=run, kind="CREATED", previous_status=None,
            )
            return WorkflowReservationResult(run=run, created=True)

    def get(self, workflow_id: UUID) -> WorkflowRunMetadata:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(workflow_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Workflow run not found")
            return self._run(row)

    def get_by_run_id(self, run_id: UUID) -> WorkflowRunMetadata:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?", (str(run_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Workflow run not found")
            return self._run(row)

    def get_by_request(self, owner_user_id: UUID, request_id: UUID) -> WorkflowRunMetadata:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM workflow_runs
                   WHERE owner_user_id = ? AND request_id = ?""",
                (str(owner_user_id), str(request_id)),
            ).fetchone()
            if row is None:
                raise NotFoundError("Workflow request not found")
            return self._run(row)

    def active_runs(self) -> tuple[WorkflowRunMetadata, ...]:
        """Return only unfinalized runs for trusted startup recovery."""
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM workflow_runs
                   WHERE status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED')
                   ORDER BY created_at, workflow_id""",
            ).fetchall()
            return tuple(self._run(row) for row in rows)

    def visible_runs_for_case(self, case_id: UUID, owner_user_id: UUID | None,
                              offset: int, limit: int) -> tuple[WorkflowRunMetadata, ...]:
        """Return a bounded audit projection without hydrating or replaying workflows."""
        if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or limit < 1:
            raise ValueError("Invalid workflow run page")
        clauses = [
            "case_id = ?",
            "status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED')",
        ]
        parameters = [str(case_id)]
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            parameters.append(str(owner_user_id))
        parameters.extend((limit, offset))
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                f"""SELECT * FROM workflow_runs
                    WHERE {' AND '.join(clauses)}
                    -- UUIDs are a stable cross-database tie-breaker when
                    -- timestamp precision produces a tie.
                    ORDER BY created_at, workflow_id
                    LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
            return tuple(self._run(row) for row in rows)

    def recent_runs(self, *, offset: int, limit: int, case_id: UUID | None = None,
                    run_id: UUID | None = None) -> tuple[WorkflowRunMetadata, ...]:
        """Return bounded newest-first durable metadata for Admin diagnostics."""
        if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 101:
            raise ValueError("Invalid recent workflow page")
        clauses = []
        parameters = []
        if case_id is not None:
            clauses.append("case_id = ?")
            parameters.append(str(case_id))
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(str(run_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        with self._persistence.transaction() as connection:
            rows = connection.execute(
                f"""SELECT * FROM workflow_runs {where}
                    ORDER BY updated_at DESC, workflow_id DESC LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
            return tuple(self._run(row) for row in rows)

    def transition(self, workflow_id: UUID, *, owner_user_id: UUID, case_id: UUID,
                   expected_status: WorkflowMetadataStatus,
                   status: WorkflowMetadataStatus,
                   failure_category: FailureCategory | None = None,
                   failure_stage: FailureStage | None = None,
                   invalid_response_stage: InvalidResponseStage | None = None,
                   invalid_response_category: InvalidResponseCategory | None = None) -> WorkflowRunMetadata:
        try:
            expected_status = WorkflowMetadataStatus(expected_status)
            status = WorkflowMetadataStatus(status)
        except ValueError:
            raise ConflictError("Invalid workflow status transition") from None
        if status not in WORKFLOW_STATUS_TRANSITIONS[expected_status]:
            raise ConflictError("Invalid workflow status transition")
        if status is WorkflowMetadataStatus.FAILED:
            if (failure_category is None) != (failure_stage is None):
                raise ConflictError("Workflow failure metadata must be paired")
            if failure_category is None:
                failure_category, failure_stage = "WORKFLOW_ERROR", "WORKFLOW"
            if ((failure_stage == "WORKFLOW" and failure_category != "WORKFLOW_ERROR")
                    or (failure_stage == "AGENT" and failure_category not in AGENT_FAILURE_CATEGORIES)):
                raise ConflictError("Invalid workflow failure metadata")
        elif failure_category is not None or failure_stage is not None:
            raise ConflictError("Workflow failure metadata requires FAILED status")
        if invalid_response_stage is not None or invalid_response_category is not None:
            if failure_category != "INVALID_RESPONSE" or failure_stage != "AGENT":
                raise ConflictError("Invalid-response diagnostics require an agent INVALID_RESPONSE")
            if (not isinstance(invalid_response_stage, str)
                    or invalid_response_stage not in {"provider_envelope", "json_guardrail", "output_schema"}):
                raise ConflictError("Invalid invalid-response stage")
            if (invalid_response_category is not None
                    and (not isinstance(invalid_response_category, str)
                         or invalid_response_stage != "json_guardrail"
                         or invalid_response_category not in {
                             "bedrock_empty_output", "bedrock_nonempty_json_rejected"})):
                raise ConflictError("Invalid invalid-response category")
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(workflow_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Workflow run not found")
            current = self._run(row)
            if current.owner_user_id != owner_user_id or current.case_id != case_id:
                raise ConflictError("Workflow does not match the selected owner and case")
            if current.status != expected_status:
                raise ConflictError("Workflow status is stale")
            updated_at = datetime.now(timezone.utc)
            if updated_at <= current.updated_at:
                updated_at = current.updated_at + timedelta(microseconds=1)
            updated = connection.execute(
                """UPDATE workflow_runs
                   SET status = ?, updated_at = ?, failure_category = ?, failure_stage = ?,
                       invalid_response_stage = ?, invalid_response_category = ?
                   WHERE workflow_id = ? AND owner_user_id = ? AND case_id = ? AND status = ?""",
                (status.value, updated_at.isoformat(), failure_category, failure_stage,
                 invalid_response_stage, invalid_response_category, str(workflow_id),
                 str(owner_user_id), str(case_id), expected_status.value),
            )
            if updated.rowcount != 1:
                raise ConflictError("Workflow status is stale")
            run = WorkflowRunMetadata.model_validate({
                **current.model_dump(), "status": status, "updated_at": updated_at,
                "failure_category": failure_category, "failure_stage": failure_stage,
                "invalid_response_stage": invalid_response_stage,
                "invalid_response_category": invalid_response_category,
            })
            self._append_run_event(
                connection, run=run,
                kind="RECOVERY_REQUIRED" if status == WorkflowMetadataStatus.RECOVERY_REQUIRED
                else "STATUS_CHANGED",
                previous_status=expected_status,
            )
            return run

    def require_recovery(self, workflow_id: UUID, *, owner_user_id: UUID,
                         case_id: UUID,
                         expected_status: WorkflowMetadataStatus) -> WorkflowRunMetadata:
        try:
            expected_status = WorkflowMetadataStatus(expected_status)
        except ValueError:
            raise ConflictError("Only an active workflow can require recovery") from None
        if expected_status not in (
            WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.RESUMING,
            WorkflowMetadataStatus.REVIEW_REQUIRED,
        ):
            raise ConflictError("Only an active workflow can require recovery")
        return self.transition(
            workflow_id, owner_user_id=owner_user_id, case_id=case_id,
            expected_status=expected_status,
            status=WorkflowMetadataStatus.RECOVERY_REQUIRED,
        )

    def events(self, workflow_id: UUID) -> tuple[WorkflowRunEvent, ...]:
        with self._persistence.transaction() as connection:
            run_row = connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (str(workflow_id),),
            ).fetchone()
            if run_row is None:
                raise NotFoundError("Workflow run not found")
            run = self._run(run_row)
            rows = connection.execute(
                """SELECT * FROM workflow_run_events
                   WHERE workflow_id = ? ORDER BY sequence""",
                (str(workflow_id),),
            ).fetchall()
            events = tuple(self._event(row) for row in rows)
            for sequence, (row, event) in enumerate(zip(rows, events), start=1):
                try:
                    payload = json.loads(row["event_json"])
                except Exception:
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None
                if (
                    event.sequence != sequence
                    or payload.get("workflow_id") != str(run.workflow_id)
                    or payload.get("run_id") != str(run.run_id)
                    or payload.get("case_id") != str(run.case_id)
                    or payload.get("owner_user_id") != str(run.owner_user_id)
                    or payload.get("sequence") != event.sequence
                    or payload.get("kind") != event.kind
                    or payload.get("previous_status") != (
                        event.previous_status.value if event.previous_status else None
                    )
                    or payload.get("status") != event.status.value
                ):
                    raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            if not events or events[-1].status != run.status:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return events
