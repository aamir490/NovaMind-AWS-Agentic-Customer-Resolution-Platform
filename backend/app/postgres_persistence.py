"""PostgreSQL persistence adapter for NovaMind's durable repositories.

The application repositories pre-date the PostgreSQL rollout and deliberately
use a small SQLite-shaped transaction surface (``execute``, ``fetchone``,
``fetchall``, and ``rowcount``).  This module implements that surface over
psycopg without weakening the application-level validation that protects HITL
workflows.  It also owns PostgreSQL-native, append-only migration guards; it
never reads, writes, or migrates a SQLite database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, ContextManager, Iterator, Literal, Mapping, Protocol, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .langgraph_checkpoints import SQLiteCheckpointSaver
from .persistence import (
    PersistenceError,
    UnitOfWork,
    _canonical_legacy_postgres_timestamp,
)
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


@dataclass(frozen=True)
class PostgreSQLMigration:
    """An immutable, ordered PostgreSQL migration."""

    version: int
    statements: tuple[str, ...]


# PostgreSQL starts at the current durable schema rather than attempting to
# replay SQLite table-rebuild migrations.  PostgreSQL has never been a
# supported NovaMind store, so there is no PostgreSQL legacy state to upgrade.
# New PostgreSQL changes must append a migration; versions are never edited.
POSTGRES_MIGRATIONS = (
    PostgreSQLMigration(1, (
        """CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL CHECK(char_length(btrim(name)) BETWEEN 1 AND 120)
        )""",
        """CREATE TABLE orders (
            order_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE order_items (
            order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            sku TEXT NOT NULL CHECK(char_length(btrim(sku)) BETWEEN 1 AND 120),
            name TEXT NOT NULL CHECK(char_length(btrim(name)) BETWEEN 1 AND 120),
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000),
            PRIMARY KEY(order_id, sku)
        )""",
        """CREATE TABLE support_cases (
            case_id TEXT PRIMARY KEY NOT NULL,
            customer_id TEXT NOT NULL REFERENCES customers(customer_id),
            order_id TEXT NOT NULL REFERENCES orders(order_id),
            subject TEXT NOT NULL CHECK(char_length(btrim(subject)) BETWEEN 1 AND 200),
            description TEXT NOT NULL CHECK(char_length(btrim(description)) BETWEEN 1 AND 4000),
            status TEXT NOT NULL CHECK(status IN ('open', 'in_review', 'escalated', 'resolved')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE trusted_delivery_sources (
            source_id TEXT PRIMARY KEY NOT NULL CHECK(char_length(btrim(source_id)) BETWEEN 1 AND 120)
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
            source_reference TEXT NOT NULL CHECK(char_length(btrim(source_reference)) BETWEEN 1 AND 120),
            verified_at TEXT NOT NULL,
            FOREIGN KEY(order_id, sku) REFERENCES order_items(order_id, sku),
            UNIQUE(case_id, delivery_fact_id), UNIQUE(case_id, sku),
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
            assessment_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id, sku) REFERENCES order_items(order_id, sku),
            FOREIGN KEY(case_id, delivery_fact_id) REFERENCES delivery_facts(case_id, delivery_fact_id),
            UNIQUE(case_id, evidence_id)
        )""",
        """CREATE TABLE resolution_proposals (
            proposal_id TEXT PRIMARY KEY NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            action TEXT NOT NULL CHECK(action IN ('refund', 'refund_investigation', 'replacement', 'return')),
            rationale TEXT NOT NULL CHECK(char_length(btrim(rationale)) BETWEEN 1 AND 4000),
            eligibility_evidence_id TEXT REFERENCES eligibility_evidence(evidence_id),
            status TEXT NOT NULL CHECK(status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reviewed_at TEXT,
            reviewer_user_id TEXT, review_note TEXT,
            CHECK((action = 'refund' AND eligibility_evidence_id IS NOT NULL)
                  OR (action <> 'refund' AND eligibility_evidence_id IS NULL)),
            CHECK((status = 'PENDING_REVIEW' AND reviewed_at IS NULL AND reviewer_user_id IS NULL AND review_note IS NULL)
                  OR (status IN ('APPROVED', 'REJECTED') AND reviewed_at IS NOT NULL
                      AND reviewer_user_id IS NOT NULL AND review_note IS NOT NULL)),
            FOREIGN KEY(case_id, eligibility_evidence_id) REFERENCES eligibility_evidence(case_id, evidence_id),
            UNIQUE(case_id, proposal_id)
        )""",
        """CREATE TABLE workflow_runs (
            workflow_id TEXT PRIMARY KEY NOT NULL, run_id TEXT UNIQUE NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id), owner_user_id TEXT NOT NULL,
            request_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
            provider TEXT NOT NULL CHECK(provider IN ('local_scripted', 'bedrock')),
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(owner_user_id, request_id), UNIQUE(workflow_id, case_id)
        )""",
        """CREATE TABLE workflow_review_bindings (
            proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES resolution_proposals(proposal_id),
            workflow_id TEXT UNIQUE NOT NULL REFERENCES workflow_runs(workflow_id),
            case_id TEXT NOT NULL REFERENCES support_cases(case_id), review_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id, proposal_id) REFERENCES resolution_proposals(case_id, proposal_id),
            FOREIGN KEY(workflow_id, case_id) REFERENCES workflow_runs(workflow_id, case_id)
        )""",
        """CREATE TABLE workflow_audit_events (
            workflow_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            kind TEXT NOT NULL CHECK(kind IN ('AGENT', 'REVIEW_REQUIRED', 'HUMAN_REVIEW', 'FAILURE')),
            proposal_id TEXT REFERENCES resolution_proposals(proposal_id), reviewer_user_id TEXT,
            proposal_status TEXT CHECK(proposal_status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            event_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(workflow_id, sequence)
        )""",
        """CREATE TABLE workflow_run_events (
            workflow_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            kind TEXT NOT NULL CHECK(kind IN ('CREATED', 'STATUS_CHANGED', 'RECOVERY_REQUIRED')),
            previous_status TEXT CHECK(previous_status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'RESUMING', 'REVIEW_REQUIRED', 'REVIEWED', 'COMPLETED', 'FAILED', 'RECOVERY_REQUIRED')),
            event_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(workflow_id, sequence)
        )""",
        """CREATE TABLE langgraph_checkpoints (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id), checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, checkpoint_type TEXT NOT NULL,
            checkpoint_blob BYTEA NOT NULL, checkpoint_hash TEXT NOT NULL CHECK(char_length(checkpoint_hash) = 64),
            metadata_type TEXT NOT NULL, metadata_blob BYTEA NOT NULL,
            metadata_hash TEXT NOT NULL CHECK(char_length(metadata_hash) = 64), created_at TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id),
            FOREIGN KEY(thread_id, checkpoint_ns, parent_checkpoint_id)
              REFERENCES langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id)
        )""",
        """CREATE TABLE langgraph_checkpoint_blobs (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id), checkpoint_ns TEXT NOT NULL,
            channel TEXT NOT NULL, version_key TEXT NOT NULL, value_type TEXT NOT NULL,
            value_blob BYTEA NOT NULL, value_hash TEXT NOT NULL CHECK(char_length(value_hash) = 64),
            PRIMARY KEY(thread_id, checkpoint_ns, channel, version_key)
        )""",
        """CREATE TABLE langgraph_checkpoint_writes (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL, write_index INTEGER NOT NULL, channel TEXT NOT NULL,
            value_type TEXT NOT NULL, value_blob BYTEA NOT NULL,
            value_hash TEXT NOT NULL CHECK(char_length(value_hash) = 64), task_path TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index),
            FOREIGN KEY(thread_id, checkpoint_ns, checkpoint_id)
              REFERENCES langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id) ON DELETE CASCADE
        )""",
        """CREATE TABLE langgraph_checkpoint_staged_writes (
            thread_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id), checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, write_index INTEGER NOT NULL,
            channel TEXT NOT NULL, value_type TEXT NOT NULL, value_blob BYTEA NOT NULL,
            value_hash TEXT NOT NULL CHECK(char_length(value_hash) = 64), task_path TEXT NOT NULL,
            PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index)
        )""",
    )),
    PostgreSQLMigration(2, (
        """CREATE TABLE case_resolution_decisions (
            decision_id TEXT PRIMARY KEY NOT NULL, case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            action TEXT NOT NULL CHECK(action IN ('APPROVE_REPLACEMENT', 'DECLINE_REPLACEMENT', 'REQUEST_MORE_EVIDENCE', 'ESCALATE')),
            reason TEXT NOT NULL CHECK(char_length(btrim(reason)) BETWEEN 1 AND 2000),
            customer_message TEXT NOT NULL CHECK(char_length(btrim(customer_message)) BETWEEN 1 AND 2000),
            admin_user_id TEXT NOT NULL CHECK(char_length(btrim(admin_user_id)) BETWEEN 1 AND 120),
            decided_at TEXT NOT NULL, UNIQUE(case_id, sequence)
        )""",
        """CREATE TABLE case_customer_communications (
            communication_id TEXT PRIMARY KEY NOT NULL, case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            message TEXT NOT NULL CHECK(char_length(btrim(message)) BETWEEN 1 AND 2000),
            admin_user_id TEXT NOT NULL CHECK(char_length(admin_user_id) = 36),
            idempotency_key TEXT NOT NULL CHECK(char_length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(char_length(request_fingerprint) = 64),
            sent_at TEXT NOT NULL, UNIQUE(case_id, sequence), UNIQUE(admin_user_id, idempotency_key)
        )""",
        """CREATE TABLE investigation_jobs (
            job_id TEXT PRIMARY KEY NOT NULL, case_id TEXT UNIQUE NOT NULL REFERENCES support_cases(case_id),
            customer_id TEXT NOT NULL REFERENCES customers(customer_id), owner_user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL CHECK(char_length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(char_length(request_fingerprint) = 64),
            status TEXT NOT NULL CHECK(status IN ('PENDING', 'CLAIMED', 'RECOVERY_REQUIRED')),
            claim_id TEXT, claimed_by TEXT, claimed_at TEXT, lease_expires_at TEXT,
            recovery_reason TEXT CHECK(recovery_reason IS NULL OR recovery_reason IN ('LEASE_EXPIRED', 'CLAIM_AMBIGUOUS')),
            workflow_id TEXT REFERENCES workflow_runs(workflow_id), bound_at TEXT,
            agent_request_json TEXT,
            agent_request_fingerprint TEXT CHECK(agent_request_fingerprint IS NULL OR char_length(agent_request_fingerprint) = 64),
            execution_attempt_id TEXT, execution_attempted_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            submission_mode TEXT NOT NULL DEFAULT 'MANUAL' CHECK(submission_mode IN ('MANUAL', 'AUTO')),
            mode_version INTEGER NOT NULL DEFAULT 1 CHECK(mode_version >= 1),
            UNIQUE(customer_id, idempotency_key),
            CHECK((status = 'PENDING' AND claim_id IS NULL AND claimed_by IS NULL AND claimed_at IS NULL
                   AND lease_expires_at IS NULL AND recovery_reason IS NULL)
               OR (status = 'CLAIMED' AND claim_id IS NOT NULL AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL
                   AND lease_expires_at IS NOT NULL AND recovery_reason IS NULL)
               OR (status = 'RECOVERY_REQUIRED' AND claim_id IS NOT NULL AND claimed_by IS NOT NULL
                   AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND recovery_reason IS NOT NULL)),
            CHECK((execution_attempt_id IS NULL) = (execution_attempted_at IS NULL))
        )""",
        "CREATE UNIQUE INDEX investigation_jobs_workflow ON investigation_jobs(workflow_id) WHERE workflow_id IS NOT NULL",
        """CREATE TABLE investigation_control (
            singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
            mode TEXT NOT NULL CHECK(mode IN ('MANUAL', 'AUTO')), version INTEGER NOT NULL CHECK(version >= 1),
            updated_by TEXT CHECK(updated_by IS NULL OR char_length(updated_by) = 36), updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE investigation_mode_audit (
            sequence INTEGER PRIMARY KEY NOT NULL CHECK(sequence >= 1),
            previous_mode TEXT CHECK(previous_mode IS NULL OR previous_mode IN ('MANUAL', 'AUTO')),
            mode TEXT NOT NULL CHECK(mode IN ('MANUAL', 'AUTO')), version INTEGER NOT NULL CHECK(version >= 1),
            admin_user_id TEXT CHECK(admin_user_id IS NULL OR char_length(admin_user_id) = 36),
            idempotency_key TEXT CHECK(idempotency_key IS NULL OR char_length(idempotency_key) = 36),
            request_fingerprint TEXT CHECK(request_fingerprint IS NULL OR char_length(request_fingerprint) = 64),
            changed_at TEXT NOT NULL, UNIQUE(admin_user_id, idempotency_key)
        )""",
        """CREATE TABLE investigation_start_requests (
            admin_user_id TEXT NOT NULL CHECK(char_length(admin_user_id) = 36),
            idempotency_key TEXT NOT NULL CHECK(char_length(idempotency_key) = 36),
            job_id TEXT UNIQUE NOT NULL REFERENCES investigation_jobs(job_id),
            request_fingerprint TEXT NOT NULL CHECK(char_length(request_fingerprint) = 64),
            requested_at TEXT NOT NULL, PRIMARY KEY(admin_user_id, idempotency_key)
        )""",
        "CREATE INDEX orders_customer ON orders(customer_id, created_at, order_id)",
        "CREATE INDEX support_cases_customer ON support_cases(customer_id, updated_at, case_id)",
        "CREATE INDEX support_cases_order ON support_cases(order_id, case_id)",
        "CREATE INDEX eligibility_evidence_case ON eligibility_evidence(case_id, created_at, evidence_id)",
        "CREATE INDEX resolution_proposals_case ON resolution_proposals(case_id, status, created_at, proposal_id)",
        "CREATE INDEX workflow_runs_case ON workflow_runs(case_id, updated_at, workflow_id)",
        "CREATE INDEX langgraph_checkpoints_latest ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC)",
        "CREATE INDEX langgraph_checkpoint_staged_writes_scope ON langgraph_checkpoint_staged_writes(thread_id, checkpoint_ns, checkpoint_id)",
        "CREATE INDEX case_resolution_decisions_case ON case_resolution_decisions(case_id, sequence)",
        "CREATE INDEX case_customer_communications_case ON case_customer_communications(case_id, sequence)",
        "CREATE INDEX investigation_jobs_case ON investigation_jobs(case_id)",
        "CREATE INDEX investigation_jobs_pending ON investigation_jobs(status, created_at, job_id)",
        "CREATE INDEX investigation_jobs_claimed ON investigation_jobs(status, lease_expires_at, job_id)",
        "CREATE INDEX investigation_jobs_submission_mode ON investigation_jobs(submission_mode, status, created_at, job_id)",
    )),
    PostgreSQLMigration(3, (
        """CREATE OR REPLACE FUNCTION novamind_valid_json(value TEXT) RETURNS BOOLEAN
           LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN PERFORM value::jsonb; RETURN true;
           EXCEPTION WHEN others THEN RETURN false; END $$""",
        "ALTER TABLE eligibility_evidence ADD CONSTRAINT eligibility_evidence_json CHECK(novamind_valid_json(assessment_json))",
        "ALTER TABLE workflow_audit_events ADD CONSTRAINT workflow_audit_events_json CHECK(novamind_valid_json(event_json))",
        "ALTER TABLE workflow_run_events ADD CONSTRAINT workflow_run_events_json CHECK(novamind_valid_json(event_json))",
        """CREATE OR REPLACE FUNCTION novamind_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'immutable NovaMind record'; END $$""",
        """CREATE OR REPLACE FUNCTION novamind_proposal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'resolution proposals are immutable'; END IF;
             IF TG_OP = 'INSERT' THEN
               IF NEW.status <> 'PENDING_REVIEW' OR NEW.updated_at <> NEW.created_at
                  OR NEW.reviewed_at IS NOT NULL OR NEW.reviewer_user_id IS NOT NULL OR NEW.review_note IS NOT NULL THEN
                 RAISE EXCEPTION 'invalid new proposal state';
               END IF;
               IF NEW.action = 'refund' AND NOT EXISTS (
                 SELECT 1 FROM eligibility_evidence e WHERE e.evidence_id = NEW.eligibility_evidence_id
                   AND e.case_id = NEW.case_id AND (e.assessment_json::jsonb ->> 'refund_eligible') = 'true'
                   AND jsonb_array_length(e.assessment_json::jsonb -> 'denial_reasons') = 0
               ) THEN RAISE EXCEPTION 'refund proposal requires eligible evidence'; END IF;
             ELSIF OLD.proposal_id <> NEW.proposal_id OR OLD.case_id <> NEW.case_id
                OR OLD.action <> NEW.action OR OLD.rationale <> NEW.rationale
                OR OLD.eligibility_evidence_id IS DISTINCT FROM NEW.eligibility_evidence_id
                OR OLD.created_at <> NEW.created_at OR OLD.status <> 'PENDING_REVIEW'
                OR NEW.status NOT IN ('APPROVED', 'REJECTED') OR NEW.updated_at <= OLD.updated_at
                OR NEW.reviewed_at IS DISTINCT FROM NEW.updated_at OR NEW.reviewer_user_id IS NULL
                OR char_length(btrim(NEW.reviewer_user_id)) NOT BETWEEN 1 AND 120
                OR NEW.review_note IS NULL OR char_length(btrim(NEW.review_note)) NOT BETWEEN 1 AND 4000 THEN
               RAISE EXCEPTION 'invalid proposal review transition';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_workflow_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'workflow runs are immutable'; END IF;
             IF TG_OP = 'INSERT' AND NEW.owner_user_id <> 'proposal-review-persistence' AND
                (NEW.status <> 'RUNNING' OR NEW.updated_at <> NEW.created_at
                 OR char_length(NEW.request_fingerprint) <> 64 OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$') THEN
               RAISE EXCEPTION 'invalid workflow run reservation';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.workflow_id <> NEW.workflow_id OR OLD.run_id <> NEW.run_id
                OR OLD.case_id <> NEW.case_id OR OLD.owner_user_id <> NEW.owner_user_id
                OR OLD.request_id <> NEW.request_id OR OLD.request_fingerprint <> NEW.request_fingerprint
                OR OLD.provider <> NEW.provider OR OLD.created_at <> NEW.created_at OR NEW.updated_at <= OLD.updated_at
                OR NOT ((OLD.status = 'RUNNING' AND NEW.status IN ('REVIEW_REQUIRED','COMPLETED','FAILED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN ('RESUMING','REVIEWED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'RESUMING' AND NEW.status IN ('REVIEW_REQUIRED','REVIEWED','FAILED','RECOVERY_REQUIRED')))) THEN
               RAISE EXCEPTION 'invalid workflow status transition';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'UPDATE' AND ((OLD.agent_request_json IS NOT NULL OR OLD.agent_request_fingerprint IS NOT NULL)
                AND (OLD.agent_request_json IS DISTINCT FROM NEW.agent_request_json
                 OR OLD.agent_request_fingerprint IS DISTINCT FROM NEW.agent_request_fingerprint)) THEN
               RAISE EXCEPTION 'investigation request descriptor is immutable';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.submission_mode <> NEW.submission_mode OR OLD.mode_version <> NEW.mode_version) THEN
               RAISE EXCEPTION 'investigation job mode snapshot is immutable';
             END IF;
             IF TG_OP = 'UPDATE' AND ((OLD.execution_attempt_id IS NOT NULL OR OLD.execution_attempted_at IS NOT NULL)
                AND (OLD.execution_attempt_id IS DISTINCT FROM NEW.execution_attempt_id
                 OR OLD.execution_attempted_at IS DISTINCT FROM NEW.execution_attempted_at)) THEN
               RAISE EXCEPTION 'investigation execution attempt is immutable';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP <> 'INSERT' OR NOT EXISTS (
               SELECT 1 FROM resolution_proposals p JOIN workflow_runs w ON w.workflow_id = NEW.workflow_id
               WHERE p.proposal_id = NEW.proposal_id AND p.case_id = NEW.case_id
                 AND p.status = 'PENDING_REVIEW' AND w.case_id = NEW.case_id AND w.status = 'REVIEW_REQUIRED'
             ) THEN RAISE EXCEPTION 'invalid workflow review binding'; END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP <> 'INSERT' OR NEW.sequence <> COALESCE((SELECT max(sequence) + 1
                 FROM case_resolution_decisions WHERE case_id = NEW.case_id), 1) THEN
               RAISE EXCEPTION 'invalid case resolution decision sequence';
             END IF;
             IF NOT EXISTS (SELECT 1 FROM support_cases c WHERE c.case_id = NEW.case_id AND
               ((NEW.action = 'REQUEST_MORE_EVIDENCE' AND c.status = 'in_review')
                OR (NEW.action IN ('APPROVE_REPLACEMENT','DECLINE_REPLACEMENT') AND c.status = 'resolved')
                OR (NEW.action = 'ESCALATE' AND c.status = 'escalated'))) THEN
               RAISE EXCEPTION 'invalid case resolution decision status';
             END IF; RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_communication_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN IF TG_OP <> 'INSERT' OR NEW.sequence <> COALESCE((SELECT max(sequence) + 1
             FROM case_customer_communications WHERE case_id = NEW.case_id), 1) THEN
             RAISE EXCEPTION 'invalid customer communication sequence'; END IF; RETURN NEW; END $$""",
        "CREATE TRIGGER proposal_guard BEFORE INSERT OR UPDATE OR DELETE ON resolution_proposals FOR EACH ROW EXECUTE FUNCTION novamind_proposal_guard()",
        "CREATE TRIGGER workflow_guard BEFORE INSERT OR UPDATE OR DELETE ON workflow_runs FOR EACH ROW EXECUTE FUNCTION novamind_workflow_guard()",
        "CREATE TRIGGER binding_guard BEFORE INSERT OR UPDATE OR DELETE ON workflow_review_bindings FOR EACH ROW EXECUTE FUNCTION novamind_binding_guard()",
        "CREATE TRIGGER decision_guard BEFORE INSERT OR UPDATE OR DELETE ON case_resolution_decisions FOR EACH ROW EXECUTE FUNCTION novamind_decision_guard()",
        "CREATE TRIGGER communication_guard BEFORE INSERT OR UPDATE OR DELETE ON case_customer_communications FOR EACH ROW EXECUTE FUNCTION novamind_communication_guard()",
        "CREATE TRIGGER job_guard BEFORE UPDATE ON investigation_jobs FOR EACH ROW EXECUTE FUNCTION novamind_job_guard()",
        "CREATE TRIGGER immutable_delivery_sources BEFORE UPDATE OR DELETE ON trusted_delivery_sources FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_delivery_facts BEFORE UPDATE OR DELETE ON delivery_facts FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_eligibility_evidence BEFORE UPDATE OR DELETE ON eligibility_evidence FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_workflow_audit BEFORE UPDATE OR DELETE ON workflow_audit_events FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_workflow_events BEFORE UPDATE OR DELETE ON workflow_run_events FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_checkpoints BEFORE UPDATE ON langgraph_checkpoints FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_checkpoint_blobs BEFORE UPDATE ON langgraph_checkpoint_blobs FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_checkpoint_writes BEFORE UPDATE ON langgraph_checkpoint_writes FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_staged_checkpoint_writes BEFORE UPDATE ON langgraph_checkpoint_staged_writes FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_investigation_audit BEFORE UPDATE OR DELETE ON investigation_mode_audit FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_investigation_start BEFORE UPDATE OR DELETE ON investigation_start_requests FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
        "CREATE TRIGGER immutable_investigation_control BEFORE DELETE ON investigation_control FOR EACH ROW EXECUTE FUNCTION novamind_immutable()",
    )),
    PostgreSQLMigration(4, (
        """INSERT INTO investigation_control(singleton, mode, version, updated_by, updated_at)
           VALUES (1, 'MANUAL', 1, NULL, CURRENT_TIMESTAMP::text)""",
        """INSERT INTO investigation_mode_audit(sequence, previous_mode, mode, version, admin_user_id,
           idempotency_key, request_fingerprint, changed_at)
           SELECT 1, NULL, mode, version, NULL, NULL, NULL, updated_at
           FROM investigation_control WHERE singleton = 1""",
    )),
    PostgreSQLMigration(5, (
        """CREATE OR REPLACE FUNCTION novamind_delivery_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP <> 'INSERT' OR NOT EXISTS (
               SELECT 1 FROM support_cases c JOIN orders o ON o.order_id = c.order_id
               JOIN order_items i ON i.order_id = o.order_id
               WHERE c.case_id = NEW.case_id AND c.order_id = NEW.order_id
                 AND c.customer_id = NEW.customer_id AND o.customer_id = NEW.customer_id
                 AND i.sku = NEW.sku AND NEW.quantity <= i.quantity
             ) THEN RAISE EXCEPTION 'invalid delivery fact bindings'; END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE snapshot jsonb;
           BEGIN
             IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'eligibility evidence is immutable'; END IF;
             snapshot := NEW.assessment_json::jsonb;
             IF NOT EXISTS (SELECT 1 FROM delivery_facts f WHERE f.delivery_fact_id = NEW.delivery_fact_id
                 AND f.case_id = NEW.case_id AND f.order_id = NEW.order_id
                 AND f.customer_id = NEW.customer_id AND f.sku = NEW.sku)
                OR snapshot ->> 'evidence_id' IS DISTINCT FROM NEW.evidence_id
                OR snapshot ->> 'case_id' IS DISTINCT FROM NEW.case_id
                OR snapshot ->> 'order_id' IS DISTINCT FROM NEW.order_id
                OR snapshot ->> 'customer_id' IS DISTINCT FROM NEW.customer_id
                OR snapshot ->> 'sku' IS DISTINCT FROM NEW.sku
                OR snapshot ->> 'policy_id' IS DISTINCT FROM NEW.policy_id
                OR snapshot ->> 'policy_version' IS DISTINCT FROM NEW.policy_version
                OR snapshot ->> 'assessment_basis' IS DISTINCT FROM 'trusted_delivery_record'
                OR snapshot ->> 'authorization_granted' IS DISTINCT FROM 'false'
                OR (snapshot ->> 'return_eligible') IS DISTINCT FROM (snapshot ->> 'refund_eligible')
                OR ((snapshot ->> 'refund_eligible') = 'true') IS DISTINCT FROM
                   (jsonb_array_length(snapshot -> 'denial_reasons') = 0)
                OR NOT EXISTS (SELECT 1 FROM delivery_facts f JOIN order_items i
                    ON i.order_id = f.order_id AND i.sku = f.sku
                    WHERE f.delivery_fact_id = NEW.delivery_fact_id
                      AND (snapshot ->> 'requested_quantity')::integer = f.quantity
                      AND (snapshot ->> 'purchased_quantity')::integer = i.quantity
                      AND snapshot ->> 'reason' = f.reason
                      AND snapshot ->> 'condition' = f.item_condition) THEN
               RAISE EXCEPTION 'invalid eligibility assessment snapshot';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_workflow_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE payload jsonb;
           BEGIN
             IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'workflow audit events are immutable'; END IF;
             payload := NEW.event_json::jsonb;
             IF NEW.sequence <> COALESCE((SELECT max(sequence) + 1 FROM workflow_audit_events
                 WHERE workflow_id = NEW.workflow_id), 1)
                OR payload ->> 'kind' IS DISTINCT FROM NEW.kind
                OR payload ->> 'workflow_id' IS DISTINCT FROM NEW.workflow_id
                OR payload ->> 'proposal_id' IS DISTINCT FROM NEW.proposal_id
                OR payload ->> 'proposal_status' IS DISTINCT FROM NEW.proposal_status
                OR payload ->> 'reviewer_user_id' IS DISTINCT FROM NEW.reviewer_user_id
                OR NOT EXISTS (SELECT 1 FROM workflow_runs w JOIN resolution_proposals p
                    ON p.proposal_id = NEW.proposal_id WHERE w.workflow_id = NEW.workflow_id
                    AND w.case_id = p.case_id AND payload ->> 'case_id' = p.case_id) THEN
               RAISE EXCEPTION 'invalid workflow audit payload';
             END IF;
             IF NEW.kind = 'REVIEW_REQUIRED' AND (NEW.proposal_id IS NULL OR NEW.reviewer_user_id IS NOT NULL
                OR NEW.proposal_status <> 'PENDING_REVIEW' OR NOT EXISTS (SELECT 1 FROM workflow_review_bindings b
                  WHERE b.workflow_id = NEW.workflow_id AND b.proposal_id = NEW.proposal_id
                    AND b.review_id = payload ->> 'review_id')) THEN
               RAISE EXCEPTION 'invalid review-required audit event';
             END IF;
             IF NEW.kind = 'HUMAN_REVIEW' AND (NEW.proposal_id IS NULL OR NEW.reviewer_user_id IS NULL
                OR NEW.proposal_status NOT IN ('APPROVED','REJECTED') OR NOT EXISTS (SELECT 1
                  FROM resolution_proposals p WHERE p.proposal_id = NEW.proposal_id
                    AND p.status = NEW.proposal_status AND p.reviewer_user_id = NEW.reviewer_user_id)) THEN
               RAISE EXCEPTION 'invalid human-review audit event';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_workflow_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE payload jsonb;
           BEGIN
             IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'workflow run events are immutable'; END IF;
             payload := NEW.event_json::jsonb;
             IF NEW.sequence <> COALESCE((SELECT max(sequence) + 1 FROM workflow_run_events
                   WHERE workflow_id = NEW.workflow_id), 1)
                OR NOT EXISTS (SELECT 1 FROM workflow_runs w WHERE w.workflow_id = NEW.workflow_id
                   AND w.status = NEW.status AND payload ->> 'case_id' = w.case_id
                   AND payload ->> 'owner_user_id' = w.owner_user_id AND payload ->> 'workflow_id' = w.workflow_id)
                OR (payload ->> 'sequence')::integer <> NEW.sequence
                OR payload ->> 'kind' IS DISTINCT FROM NEW.kind
                OR payload ->> 'previous_status' IS DISTINCT FROM NEW.previous_status
                OR payload ->> 'status' IS DISTINCT FROM NEW.status THEN
               RAISE EXCEPTION 'invalid workflow run event';
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE OR REPLACE FUNCTION novamind_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'UPDATE' AND ((OLD.agent_request_json IS NOT NULL OR OLD.agent_request_fingerprint IS NOT NULL)
                AND (OLD.agent_request_json IS DISTINCT FROM NEW.agent_request_json
                 OR OLD.agent_request_fingerprint IS DISTINCT FROM NEW.agent_request_fingerprint)) THEN
               RAISE EXCEPTION 'investigation request descriptor is immutable';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.submission_mode <> NEW.submission_mode OR OLD.mode_version <> NEW.mode_version) THEN
               RAISE EXCEPTION 'investigation job mode snapshot is immutable';
             END IF;
             IF TG_OP = 'UPDATE' AND ((OLD.execution_attempt_id IS NOT NULL OR OLD.execution_attempted_at IS NOT NULL)
                AND (OLD.execution_attempt_id IS DISTINCT FROM NEW.execution_attempt_id
                 OR OLD.execution_attempted_at IS DISTINCT FROM NEW.execution_attempted_at)) THEN
               RAISE EXCEPTION 'investigation execution attempt is immutable';
             END IF;
             IF TG_OP = 'UPDATE' AND OLD.status <> NEW.status AND NOT
               ((OLD.status = 'PENDING' AND NEW.status = 'CLAIMED')
                OR (OLD.status = 'CLAIMED' AND NEW.status = 'RECOVERY_REQUIRED')) THEN
               RAISE EXCEPTION 'invalid investigation job transition';
             END IF;
             IF TG_OP = 'UPDATE' AND OLD.status <> 'PENDING' AND
                (OLD.claim_id <> NEW.claim_id OR OLD.claimed_by <> NEW.claimed_by
                 OR OLD.claimed_at <> NEW.claimed_at OR OLD.lease_expires_at <> NEW.lease_expires_at) THEN
               RAISE EXCEPTION 'investigation claim is immutable';
             END IF;
             RETURN NEW;
           END $$""",
        "CREATE TRIGGER delivery_fact_guard BEFORE INSERT OR UPDATE OR DELETE ON delivery_facts FOR EACH ROW EXECUTE FUNCTION novamind_delivery_fact_guard()",
        "CREATE TRIGGER evidence_guard BEFORE INSERT OR UPDATE OR DELETE ON eligibility_evidence FOR EACH ROW EXECUTE FUNCTION novamind_evidence_guard()",
        "CREATE TRIGGER workflow_audit_guard BEFORE INSERT OR UPDATE OR DELETE ON workflow_audit_events FOR EACH ROW EXECUTE FUNCTION novamind_workflow_audit_guard()",
        "CREATE TRIGGER workflow_event_guard BEFORE INSERT OR UPDATE OR DELETE ON workflow_run_events FOR EACH ROW EXECUTE FUNCTION novamind_workflow_event_guard()",
    )),
    PostgreSQLMigration(6, (
        """CREATE TABLE cognito_customer_bindings (
            subject_id TEXT PRIMARY KEY NOT NULL CHECK(char_length(subject_id) = 36),
            customer_id TEXT UNIQUE NOT NULL REFERENCES customers(customer_id),
            created_at TEXT NOT NULL
        )""",
        """CREATE TRIGGER immutable_cognito_customer_bindings
           BEFORE UPDATE OR DELETE ON cognito_customer_bindings
           FOR EACH ROW EXECUTE FUNCTION novamind_immutable()""",
    )),
    PostgreSQLMigration(7, (
        # Migration 4 used PostgreSQL's ``CURRENT_TIMESTAMP::text`` rendering
        # (for example, a ``+00`` offset).  It is a valid PostgreSQL timestamp
        # but not the canonical ISO-8601 representation required by the
        # validated domain records.  Normalize the mutable singleton rather
        # than weakening its reader.  The historical audit row is protected by
        # the append-only trigger and is normalized at its strict read boundary.
        """UPDATE investigation_control
           SET updated_at = to_char(
               updated_at::timestamptz AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
           )
           WHERE singleton = 1""",
    )),
    PostgreSQLMigration(8, (
        """ALTER TABLE workflow_runs ADD COLUMN failure_category TEXT
           CHECK(failure_category IS NULL OR failure_category IN
             ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
              'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR','WORKFLOW_ERROR'))""",
        """ALTER TABLE workflow_runs ADD COLUMN failure_stage TEXT
           CHECK(failure_stage IS NULL OR failure_stage IN ('AGENT','WORKFLOW'))""",
        """ALTER TABLE workflow_runs ADD CONSTRAINT workflow_failure_metadata_paired
           CHECK((failure_category IS NULL) = (failure_stage IS NULL))""",
        """CREATE OR REPLACE FUNCTION novamind_workflow_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'workflow runs are immutable'; END IF;
             IF TG_OP = 'INSERT' AND NEW.owner_user_id <> 'proposal-review-persistence' AND
                (NEW.status <> 'RUNNING' OR NEW.updated_at <> NEW.created_at
                 OR char_length(NEW.request_fingerprint) <> 64 OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$'
                 OR NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL) THEN
               RAISE EXCEPTION 'invalid workflow run reservation';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.workflow_id <> NEW.workflow_id OR OLD.run_id <> NEW.run_id
                OR OLD.case_id <> NEW.case_id OR OLD.owner_user_id <> NEW.owner_user_id
                OR OLD.request_id <> NEW.request_id OR OLD.request_fingerprint <> NEW.request_fingerprint
                OR OLD.provider <> NEW.provider OR OLD.created_at <> NEW.created_at OR NEW.updated_at <= OLD.updated_at
                OR NOT ((OLD.status = 'RUNNING' AND NEW.status IN ('REVIEW_REQUIRED','COMPLETED','FAILED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN ('RESUMING','REVIEWED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'RESUMING' AND NEW.status IN ('REVIEW_REQUIRED','REVIEWED','FAILED','RECOVERY_REQUIRED')))
                OR (NEW.status = 'FAILED' AND NOT (
                    (NEW.failure_stage = 'WORKFLOW' AND NEW.failure_category = 'WORKFLOW_ERROR') OR
                    (NEW.failure_stage = 'AGENT' AND NEW.failure_category IN
                      ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
                       'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR'))))
                OR (NEW.status <> 'FAILED' AND
                    (NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL))) THEN
               RAISE EXCEPTION 'invalid workflow status transition';
             END IF;
             RETURN NEW;
           END $$""",
    )),
    PostgreSQLMigration(9, (
        """CREATE OR REPLACE FUNCTION novamind_workflow_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'workflow runs are immutable'; END IF;
             IF TG_OP = 'INSERT' AND NEW.owner_user_id <> 'proposal-review-persistence' AND
                (NEW.status <> 'RUNNING' OR NEW.updated_at <> NEW.created_at
                 OR char_length(NEW.request_fingerprint) <> 64 OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$'
                 OR NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL) THEN
               RAISE EXCEPTION 'invalid workflow run reservation';
             END IF;
             -- Revision 5 predates failure metadata.  During an ECS rolling
             -- deployment it can write only this exact null/null FAILED
             -- transition.  Canonicalize it to a safe generic value before
             -- enforcing the same closed transition rules used by revision 8.
             IF TG_OP = 'UPDATE' AND NEW.status = 'FAILED'
                AND NEW.failure_category IS NULL AND NEW.failure_stage IS NULL THEN
               NEW.failure_category := 'WORKFLOW_ERROR';
               NEW.failure_stage := 'WORKFLOW';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.workflow_id <> NEW.workflow_id OR OLD.run_id <> NEW.run_id
                OR OLD.case_id <> NEW.case_id OR OLD.owner_user_id <> NEW.owner_user_id
                OR OLD.request_id <> NEW.request_id OR OLD.request_fingerprint <> NEW.request_fingerprint
                OR OLD.provider <> NEW.provider OR OLD.created_at <> NEW.created_at OR NEW.updated_at <= OLD.updated_at
                OR NOT ((OLD.status = 'RUNNING' AND NEW.status IN ('REVIEW_REQUIRED','COMPLETED','FAILED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN ('RESUMING','REVIEWED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'RESUMING' AND NEW.status IN ('REVIEW_REQUIRED','REVIEWED','FAILED','RECOVERY_REQUIRED')))
                OR (NEW.status = 'FAILED' AND NOT (
                    (NEW.failure_stage = 'WORKFLOW' AND NEW.failure_category = 'WORKFLOW_ERROR') OR
                    (NEW.failure_stage = 'AGENT' AND NEW.failure_category IN
                      ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
                       'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR'))))
                OR (NEW.status <> 'FAILED' AND
                    (NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL))) THEN
               RAISE EXCEPTION 'invalid workflow status transition';
             END IF;
             RETURN NEW;
           END $$""",
    )),
    PostgreSQLMigration(10, (
        """ALTER TABLE workflow_runs ADD COLUMN invalid_response_stage TEXT
           CHECK(invalid_response_stage IS NULL OR invalid_response_stage IN
             ('provider_envelope','json_guardrail','output_schema'))""",
        """ALTER TABLE workflow_runs ADD COLUMN invalid_response_category TEXT
           CHECK(invalid_response_category IS NULL OR invalid_response_category IN
             ('bedrock_empty_output','bedrock_nonempty_json_rejected'))""",
        """CREATE OR REPLACE FUNCTION novamind_workflow_guard() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'workflow runs are immutable'; END IF;
             IF TG_OP = 'INSERT' AND NEW.owner_user_id <> 'proposal-review-persistence' AND
                (NEW.status <> 'RUNNING' OR NEW.updated_at <> NEW.created_at
                 OR char_length(NEW.request_fingerprint) <> 64 OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$'
                 OR NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL
                 OR NEW.invalid_response_stage IS NOT NULL OR NEW.invalid_response_category IS NOT NULL) THEN
               RAISE EXCEPTION 'invalid workflow run reservation';
             END IF;
             IF TG_OP = 'UPDATE' AND NEW.status = 'FAILED'
                AND NEW.failure_category IS NULL AND NEW.failure_stage IS NULL THEN
               NEW.failure_category := 'WORKFLOW_ERROR';
               NEW.failure_stage := 'WORKFLOW';
             END IF;
             IF TG_OP = 'UPDATE' AND (OLD.workflow_id <> NEW.workflow_id OR OLD.run_id <> NEW.run_id
                OR OLD.case_id <> NEW.case_id OR OLD.owner_user_id <> NEW.owner_user_id
                OR OLD.request_id <> NEW.request_id OR OLD.request_fingerprint <> NEW.request_fingerprint
                OR OLD.provider <> NEW.provider OR OLD.created_at <> NEW.created_at OR NEW.updated_at <= OLD.updated_at
                OR NOT ((OLD.status = 'RUNNING' AND NEW.status IN ('REVIEW_REQUIRED','COMPLETED','FAILED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'REVIEW_REQUIRED' AND NEW.status IN ('RESUMING','REVIEWED','RECOVERY_REQUIRED'))
                     OR (OLD.status = 'RESUMING' AND NEW.status IN ('REVIEW_REQUIRED','REVIEWED','FAILED','RECOVERY_REQUIRED')))
                OR (NEW.status = 'FAILED' AND NOT (
                    (NEW.failure_stage = 'WORKFLOW' AND NEW.failure_category = 'WORKFLOW_ERROR') OR
                    (NEW.failure_stage = 'AGENT' AND NEW.failure_category IN
                      ('TIMEOUT','UNAVAILABLE','REFUSED','INCOMPLETE','INVALID_RESPONSE','INPUT_LIMIT',
                       'TOOL_ERROR','LLM_ERROR','STEP_LIMIT','GRAPH_ERROR'))))
                OR (NEW.status <> 'FAILED' AND
                    (NEW.failure_category IS NOT NULL OR NEW.failure_stage IS NOT NULL
                     OR NEW.invalid_response_stage IS NOT NULL OR NEW.invalid_response_category IS NOT NULL))
                OR ((NEW.invalid_response_stage IS NOT NULL OR NEW.invalid_response_category IS NOT NULL) AND NOT (
                    NEW.failure_category = 'INVALID_RESPONSE' AND NEW.failure_stage = 'AGENT'
                    AND NEW.invalid_response_stage IN ('provider_envelope','json_guardrail','output_schema')
                    AND (NEW.invalid_response_category IS NULL OR
                         (NEW.invalid_response_stage = 'json_guardrail' AND NEW.invalid_response_category IN
                           ('bedrock_empty_output','bedrock_nonempty_json_rejected')))))) THEN
               RAISE EXCEPTION 'invalid workflow status transition';
             END IF;
             RETURN NEW;
           END $$""",
    )),
    PostgreSQLMigration(11, (
        """CREATE TABLE investigation_results (
            result_id TEXT PRIMARY KEY NOT NULL,
            job_id TEXT UNIQUE NOT NULL REFERENCES investigation_jobs(job_id),
            workflow_id TEXT UNIQUE NOT NULL REFERENCES workflow_runs(workflow_id),
            execution_attempt_id TEXT UNIQUE NOT NULL,
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            status TEXT NOT NULL CHECK(status IN ('COMPLETED')),
            result_json TEXT NOT NULL,
            result_hash TEXT NOT NULL CHECK(char_length(result_hash) = 64),
            actions_executed BOOLEAN NOT NULL DEFAULT FALSE CHECK(actions_executed = FALSE),
            created_at TEXT NOT NULL
        )""",
    )),
    PostgreSQLMigration(12, (
        """CREATE TABLE investigation_outbox (
            outbox_id TEXT PRIMARY KEY NOT NULL,
            job_id TEXT NOT NULL REFERENCES investigation_jobs(job_id),
            case_id TEXT NOT NULL REFERENCES support_cases(case_id),
            event_type TEXT NOT NULL CHECK(event_type = 'INVESTIGATION_DISPATCH_REQUESTED'),
            event_version INTEGER NOT NULL CHECK(event_version = 1),
            idempotency_key TEXT NOT NULL CHECK(char_length(idempotency_key) = 36),
            request_fingerprint TEXT NOT NULL CHECK(char_length(request_fingerprint) = 64),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING', 'PUBLISHED')),
            publication_attempts INTEGER NOT NULL DEFAULT 0 CHECK(publication_attempts >= 0),
            last_error_category TEXT,
            message_id TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT,
            UNIQUE(job_id, event_type),
            CHECK((status = 'PENDING' AND published_at IS NULL)
               OR (status = 'PUBLISHED' AND published_at IS NOT NULL))
        )""",
        "CREATE INDEX investigation_outbox_pending ON investigation_outbox(status, created_at, outbox_id)",
        "CREATE INDEX investigation_outbox_job ON investigation_outbox(job_id, event_type)",
    )),
    PostgreSQLMigration(13, (
        "ALTER TABLE investigation_outbox ADD COLUMN lease_id TEXT",
        "ALTER TABLE investigation_outbox ADD COLUMN leased_by TEXT",
        "ALTER TABLE investigation_outbox ADD COLUMN lease_expires_at TEXT",
        "ALTER TABLE investigation_outbox ADD COLUMN next_attempt_at TEXT",
        "ALTER TABLE investigation_outbox ADD COLUMN retry_exhausted BOOLEAN NOT NULL DEFAULT FALSE CHECK(retry_exhausted = FALSE OR status = 'PENDING')",
        "CREATE INDEX investigation_outbox_claimable ON investigation_outbox(status, retry_exhausted, next_attempt_at, lease_expires_at, created_at, outbox_id)",
    )),
)


@dataclass(frozen=True)
class InvestigationOutboxEvent:
    """Durable intent to publish one investigation dispatch message.

    This is intentionally an intent record, not an SQS receipt.  Publication
    and acknowledgement belong to later migration phases; the business write
    and this row are committed together.
    """

    outbox_id: UUID
    job_id: UUID
    case_id: UUID
    event_type: str
    event_version: int
    idempotency_key: UUID
    request_fingerprint: str
    payload_json: str
    status: str
    publication_attempts: int
    last_error_category: str | None
    message_id: str | None
    created_at: str
    published_at: str | None
    lease_id: UUID | None
    leased_by: UUID | None
    lease_expires_at: str | None
    next_attempt_at: str | None
    retry_exhausted: bool


class _PostgreSQLRow(dict):
    """dict-row with the indexing and iteration semantics of ``sqlite3.Row``."""

    def __getitem__(self, key: str | int):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self.values())


def _qmark_to_pyformat(statement: str) -> str:
    """Translate only bind markers outside SQL string and identifier quotes."""

    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote is not None:
            result.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    result.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            result.append(char)
        elif char == "?":
            result.append("%s")
        else:
            result.append(char)
        index += 1
    if quote is not None:
        raise PersistenceError("PERSISTENCE_UNAVAILABLE")
    return "".join(result)


class _PostgreSQLCursor:
    # Repository reads historically used SQLite's implicit ``rowid`` to retain
    # insertion order.  PostgreSQL tables do not expose that implementation
    # detail; repositories select their portable ordering when this facade is
    # in use.
    supports_rowid = False

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, statement: str, parameters: Sequence[Any] | None = None):
        try:
            self._cursor.execute(_qmark_to_pyformat(statement), parameters)
        except Exception as error:
            # Repository code intentionally catches sqlite integrity errors for
            # idempotency/checkpoint conflicts.  Preserve that narrow contract
            # while mapping all other driver errors at the UoW boundary.
            state = getattr(error, "sqlstate", "") or ""
            if state.startswith("23") or state == "P0001":
                raise sqlite3.IntegrityError("PostgreSQL integrity violation") from None
            raise sqlite3.OperationalError("PostgreSQL operation failed") from None
        return self

    def executemany(self, statement: str, parameters: Sequence[Sequence[Any]]):
        """Preserve the DB-API batch-insert surface used by order creation."""

        try:
            self._cursor.executemany(_qmark_to_pyformat(statement), parameters)
        except Exception as error:
            # Keep conflict handling identical to execute(): repository code
            # intentionally catches SQLite's integrity exception type.
            state = getattr(error, "sqlstate", "") or ""
            if state.startswith("23") or state == "P0001":
                raise sqlite3.IntegrityError("PostgreSQL integrity violation") from None
            raise sqlite3.OperationalError("PostgreSQL operation failed") from None
        return self

    @staticmethod
    def _row(value: Mapping[str, Any] | None):
        return None if value is None else _PostgreSQLRow(value)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PostgreSQLPersistence:
    """Short-lived PostgreSQL transactions with guarded migrations."""

    supports_skip_locked = True

    def __init__(self, dsn: str, *, initialize: bool = False,
                 connection_factory: Callable[[str], Any] | None = None) -> None:
        if not self._valid_dsn(dsn):
            raise PersistenceError("PERSISTENCE_UNAVAILABLE")
        self._dsn = dsn
        self._connection_factory = connection_factory
        try:
            if initialize:
                self._migrate()
            self._validate()
        except PersistenceError:
            raise
        except Exception:
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None

    @staticmethod
    def _valid_dsn(value: Any) -> bool:
        if not isinstance(value, str) or not value or len(value) > 4096 or any(c.isspace() for c in value):
            return False
        parsed = urlsplit(value)
        return parsed.scheme in ("postgres", "postgresql") and bool(parsed.hostname) and bool(parsed.path.strip("/"))

    def _connect(self):
        if self._connection_factory is not None:
            return self._connection_factory(self._dsn)
        try:
            from psycopg import connect
        except ImportError:
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None
        return connect(self._dsn, connect_timeout=5, autocommit=False)

    @contextmanager
    def transaction(self) -> Iterator[_PostgreSQLCursor]:
        connection = None
        cursor = None
        try:
            connection = self._connect()
            try:
                from psycopg.rows import dict_row
                cursor = connection.cursor(row_factory=dict_row)
            except ImportError:
                # Connection factories in offline regression tests do not need
                # psycopg installed; production always uses dict rows.
                cursor = connection.cursor()
            with connection.transaction():
                wrapped = _PostgreSQLCursor(cursor)
                # ``SKIP LOCKED`` workers require READ COMMITTED: SERIALIZABLE
                # may abort otherwise independent consumers at commit with
                # SQLSTATE 40001.  Row locks, immutable state snapshots, and
                # conditional updates retain the single-winner guarantees for
                # claims and HITL transitions.
                wrapped.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                yield wrapped
        except PersistenceError:
            raise
        # These are repository outcomes, not driver failures.  In particular,
        # a HITL review with the wrong persisted workflow/review pair must
        # remain a conflict rather than masquerading as an unavailable store.
        except (ConflictError, NotFoundError):
            raise
        except sqlite3.Error:
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None
        except Exception:
            raise PersistenceError("PERSISTENCE_UNAVAILABLE") from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('novamind-postgres-schema-v1'))")
            connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL
            )""")
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
            known = {migration.version for migration in POSTGRES_MIGRATIONS}
            if applied - known:
                raise PersistenceError("PERSISTENCE_UNAVAILABLE")
            for migration in POSTGRES_MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.version, datetime.now(timezone.utc).isoformat()),
                )

    def _validate(self) -> None:
        with self.transaction() as connection:
            rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            versions = [row[0] for row in rows]
            if versions != [migration.version for migration in POSTGRES_MIGRATIONS]:
                raise PersistenceError("PERSISTENCE_UNAVAILABLE")
            for table in (
                "customers", "support_cases", "workflow_runs", "workflow_audit_events",
                "investigation_jobs", "langgraph_checkpoints", "langgraph_checkpoint_writes",
                "cognito_customer_bindings", "investigation_results", "investigation_outbox",
            ):
                connection.execute(f"SELECT 1 FROM {table} LIMIT 0")


class PostgreSQLInvestigationOutboxRepository:
    """PostgreSQL-native durable investigation dispatch intent repository."""

    EVENT_TYPE = "INVESTIGATION_DISPATCH_REQUESTED"
    EVENT_VERSION = 1

    @staticmethod
    def _event(row: Mapping[str, Any]) -> InvestigationOutboxEvent:
        try:
            return InvestigationOutboxEvent(
                outbox_id=UUID(str(row["outbox_id"])),
                job_id=UUID(str(row["job_id"])),
                case_id=UUID(str(row["case_id"])),
                event_type=str(row["event_type"]),
                event_version=int(row["event_version"]),
                idempotency_key=UUID(str(row["idempotency_key"])),
                request_fingerprint=str(row["request_fingerprint"]),
                payload_json=str(row["payload_json"]),
                status=str(row["status"]),
                publication_attempts=int(row["publication_attempts"]),
                last_error_category=row["last_error_category"],
                message_id=row["message_id"],
                created_at=str(row["created_at"]),
                published_at=row["published_at"],
                lease_id=None if row["lease_id"] is None else UUID(str(row["lease_id"])),
                leased_by=None if row["leased_by"] is None else UUID(str(row["leased_by"])),
                lease_expires_at=row["lease_expires_at"],
                next_attempt_at=row["next_attempt_at"],
                retry_exhausted=bool(row["retry_exhausted"]),
            )
        except (KeyError, TypeError, ValueError):
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR") from None

    @classmethod
    def enqueue_in_transaction(cls, connection: _PostgreSQLCursor, *, job_id: UUID,
                               case_id: UUID, idempotency_key: UUID,
                               request_fingerprint: str,
                               created_at: datetime) -> InvestigationOutboxEvent:
        """Insert or return the one dispatch intent for a job.

        Callers must invoke this inside the same PostgreSQL transaction as the
        job/business mutation.  The payload is deliberately reference-only;
        SQS publication is a later phase.
        """
        if len(request_fingerprint) != 64:
            raise ValueError("request_fingerprint must be a SHA-256 hex digest")
        payload_json = json.dumps({
            "event_type": cls.EVENT_TYPE,
            "event_version": cls.EVENT_VERSION,
            "job_id": str(job_id),
            "case_id": str(case_id),
            "outbox_id": "pending",
        }, sort_keys=True, separators=(",", ":"))
        existing = connection.execute(
            """SELECT * FROM investigation_outbox
               WHERE job_id = ? AND event_type = ?""",
            (str(job_id), cls.EVENT_TYPE),
        ).fetchone()
        if existing is not None:
            if (existing["case_id"] != str(case_id)
                    or existing["idempotency_key"] != str(idempotency_key)
                    or existing["request_fingerprint"] != request_fingerprint):
                raise ConflictError("Investigation outbox intent conflicts with the existing job")
            return cls._event(existing)

        outbox_id = uuid4()
        payload_json = payload_json.replace('"pending"', json.dumps(str(outbox_id)))
        connection.execute(
            """INSERT INTO investigation_outbox
               (outbox_id, job_id, case_id, event_type, event_version,
                idempotency_key, request_fingerprint, payload_json, status,
                publication_attempts, last_error_category, message_id,
                created_at, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (job_id, event_type) DO NOTHING""",
            (str(outbox_id), str(job_id), str(case_id), cls.EVENT_TYPE,
             cls.EVENT_VERSION, str(idempotency_key), request_fingerprint,
             payload_json, "PENDING", 0, None, None, created_at.isoformat(), None),
        )
        row = connection.execute(
            """SELECT * FROM investigation_outbox WHERE outbox_id = ?""",
            (str(outbox_id),),
        ).fetchone()
        if row is None:
            raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
        return cls._event(row)

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
        self._persistence = persistence

    def get_by_job(self, job_id: UUID) -> InvestigationOutboxEvent:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM investigation_outbox
                   WHERE job_id = ? AND event_type = ?""",
                (str(job_id), self.EVENT_TYPE),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation outbox event not found")
            return self._event(row)

    def claim_pending(self, *, publisher_id: UUID, lease_seconds: int = 30,
                      max_attempts: int = 5,
                      now: datetime | None = None) -> InvestigationOutboxEvent | None:
        """Lease one unpublished event; no external call occurs in this transaction."""
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("lease_seconds and max_attempts must be positive")
        observed = datetime.now(timezone.utc) if now is None else now
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        observed = observed.astimezone(timezone.utc)
        expires = observed + timedelta(seconds=lease_seconds)
        lease_id = uuid4()
        with self._persistence.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM investigation_outbox
                   WHERE status = 'PENDING'
                     AND retry_exhausted = FALSE
                     AND publication_attempts < ?
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                   ORDER BY created_at, outbox_id
                   FOR UPDATE SKIP LOCKED LIMIT 1""",
                (max_attempts, observed.isoformat(), observed.isoformat()),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE investigation_outbox
                   SET lease_id = ?, leased_by = ?, lease_expires_at = ?,
                       publication_attempts = publication_attempts + 1
                   WHERE outbox_id = ? AND status = 'PENDING'
                     AND retry_exhausted = FALSE
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (str(lease_id), str(publisher_id), expires.isoformat(),
                 row["outbox_id"], observed.isoformat()),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation outbox lease is stale")
            claimed = connection.execute(
                "SELECT * FROM investigation_outbox WHERE outbox_id = ?",
                (row["outbox_id"],),
            ).fetchone()
            if claimed is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return self._event(claimed)

    def mark_published(self, *, outbox_id: UUID, publisher_id: UUID,
                       lease_id: UUID, message_id: str,
                       now: datetime | None = None) -> InvestigationOutboxEvent:
        """Commit SQS confirmation only when the publisher lease still owns the row."""
        if not message_id or not isinstance(message_id, str):
            raise ValueError("message_id must be non-empty")
        observed = datetime.now(timezone.utc) if now is None else now
        with self._persistence.transaction() as connection:
            updated = connection.execute(
                """UPDATE investigation_outbox
                   SET status = 'PUBLISHED', message_id = ?, published_at = ?,
                       lease_id = NULL, leased_by = NULL, lease_expires_at = NULL,
                       next_attempt_at = NULL, last_error_category = NULL
                   WHERE outbox_id = ? AND status = 'PENDING'
                     AND leased_by = ? AND lease_id = ?
                     AND lease_expires_at > ?""",
                (message_id, observed.isoformat(), str(outbox_id),
                 str(publisher_id), str(lease_id), observed.isoformat()),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation outbox publication lease is stale")
            row = connection.execute(
                "SELECT * FROM investigation_outbox WHERE outbox_id = ?",
                (str(outbox_id),),
            ).fetchone()
            if row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return self._event(row)

    def record_publish_failure(self, *, outbox_id: UUID, publisher_id: UUID,
                               lease_id: UUID, error_category: str,
                               retry_after_seconds: int = 5,
                               max_attempts: int = 5,
                               now: datetime | None = None) -> InvestigationOutboxEvent:
        """Release a fenced lease with bounded retry metadata."""
        if not error_category or retry_after_seconds < 0 or max_attempts < 1:
            raise ValueError("invalid publication failure parameters")
        observed = datetime.now(timezone.utc) if now is None else now
        with self._persistence.transaction() as connection:
            row = connection.execute(
                """SELECT publication_attempts FROM investigation_outbox
                   WHERE outbox_id = ? AND status = 'PENDING'
                     AND leased_by = ? AND lease_id = ?
                     AND lease_expires_at > ? FOR UPDATE""",
                (str(outbox_id), str(publisher_id), str(lease_id), observed.isoformat()),
            ).fetchone()
            if row is None:
                raise ConflictError("Investigation outbox failure lease is stale")
            exhausted = int(row["publication_attempts"]) >= max_attempts
            next_attempt = None if exhausted else (
                observed + timedelta(seconds=retry_after_seconds)
            ).isoformat()
            updated = connection.execute(
                """UPDATE investigation_outbox
                   SET last_error_category = ?, next_attempt_at = ?,
                       retry_exhausted = ?, lease_id = NULL, leased_by = NULL,
                       lease_expires_at = NULL
                   WHERE outbox_id = ? AND status = 'PENDING'
                     AND leased_by = ? AND lease_id = ?""",
                (error_category, next_attempt, exhausted, str(outbox_id),
                 str(publisher_id), str(lease_id)),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation outbox failure lease is stale")
            result = connection.execute(
                "SELECT * FROM investigation_outbox WHERE outbox_id = ?",
                (str(outbox_id),),
            ).fetchone()
            if result is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            return self._event(result)


# These repository implementations intentionally duplicate the SQLite method
# surface for now, but execute directly through PostgreSQLPersistence. No
# PostgreSQL business repository inherits from or delegates to SQLite.
class PostgreSQLCaseRepository:
    """Durable customer, order, and case repository built on the M5B UoW."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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

class PostgreSQLCognitoCustomerBindingRepository:
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
            PostgreSQLCaseRepository._require_customer(connection, binding.customer_id)
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

class PostgreSQLInvestigationControlRepository:
    """Singleton mode control plus immutable administrative audit history."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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

class PostgreSQLInvestigationJobRepository:
    """Durable, idempotent complaint-intake queue; it never dispatches work."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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
        run = PostgreSQLWorkflowMetadataRepository._run(row)
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
        PostgreSQLWorkflowMetadataRepository._append_run_event(
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
                return PostgreSQLCaseRepository._case(case_row), existing, False

            PostgreSQLCaseRepository._require_customer(connection, case.customer_id)
            order = PostgreSQLCaseRepository._require_order(connection, case.order_id)
            if order.customer_id != case.customer_id:
                raise ConflictError("Order does not belong to the selected customer")
            control_row = connection.execute(
                "SELECT * FROM investigation_control WHERE singleton = 1",
            ).fetchone()
            if control_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            control = PostgreSQLInvestigationControlRepository._control(control_row)
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
            # AUTO submissions are dispatchable at intake.  MANUAL jobs remain
            # pending until an authorized ADMIN registers the start below.
            if job.submission_mode is InvestigationMode.AUTO:
                PostgreSQLInvestigationOutboxRepository.enqueue_in_transaction(
                    connection, job_id=job.id, case_id=job.case_id,
                    idempotency_key=job.idempotency_key,
                    request_fingerprint=job.request_fingerprint,
                    created_at=job.created_at,
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
            PostgreSQLInvestigationOutboxRepository.enqueue_in_transaction(
                connection, job_id=job.id, case_id=job.case_id,
                idempotency_key=idempotency_key,
                request_fingerprint=job.request_fingerprint,
                created_at=timestamp,
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
                run = PostgreSQLWorkflowMetadataRepository._run(run_row)
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
                    PostgreSQLWorkflowMetadataRepository._append_run_event(
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
                run = PostgreSQLWorkflowMetadataRepository._run(run_row)
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
                run = PostgreSQLWorkflowMetadataRepository._run(run_row)
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
                run = PostgreSQLWorkflowMetadataRepository._run(run_row)
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

class PostgreSQLRefundEvidenceRepository:
    """Opt-in durable trusted-delivery and refund-evidence repository."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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

class PostgreSQLProposalRepository:
    """Opt-in proposal persistence with atomic, append-only review decisions."""

    _ANCHOR_OWNER = "proposal-review-persistence"

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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
            # Approval/rejection is a durable proposal decision only.  It never
            # authorizes or records an external business action.
            "actions_executed": False,
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
                "SELECT * FROM resolution_proposals WHERE proposal_id = ?",
                (str(proposal_id),),
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
                "SELECT * FROM resolution_proposals WHERE proposal_id = ? FOR UPDATE",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Proposal not found")
            proposal = self._proposal(row)
            if proposal.status != ProposalStatus.PENDING_REVIEW:
                raise ConflictError("Proposal has already been reviewed")

            binding_row = connection.execute(
                "SELECT * FROM workflow_review_bindings WHERE proposal_id = ? FOR UPDATE",
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
                       FROM workflow_runs WHERE workflow_id = ? FOR UPDATE""",
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
                PostgreSQLWorkflowMetadataRepository._append_run_event(
                    connection, run=PostgreSQLWorkflowMetadataRepository._run(updated_run),
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


class PostgreSQLInvestigationResultRepository:
    """One-shot durable Strands result commit fenced by job claim and attempt."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
        self._persistence = persistence

    def record_completed(self, job_id: UUID, *, worker_id: UUID, claim_id: UUID,
                         attempt_id: UUID, result, now: datetime | None = None) -> dict:
        observed_at = datetime.now(timezone.utc) if now is None else now
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Result timestamps must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)
        payload = result.model_dump(mode="json")
        payload["actions_executed"] = False
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        result_hash = sha256(serialized.encode("utf-8")).hexdigest()
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigation_jobs WHERE job_id = ? FOR UPDATE",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise NotFoundError("Investigation job not found")
            job = PostgreSQLInvestigationJobRepository._job(row)
            if (job.claim_id != claim_id or job.claimed_by != worker_id
                    or job.execution_attempt_id != attempt_id
                    or job.workflow_id is None
                    or job.status != InvestigationJobStatus.CLAIMED
                    or job.lease_expires_at is None or job.lease_expires_at <= observed_at):
                raise ConflictError("Investigation result fence is no longer active")
            existing = connection.execute(
                "SELECT result_json, result_hash FROM investigation_results WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if existing is not None:
                if existing["result_hash"] != result_hash:
                    raise ConflictError("Investigation result conflicts with the committed attempt")
                return json.loads(existing["result_json"])
            run_row = connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? FOR UPDATE",
                (str(job.workflow_id),),
            ).fetchone()
            if run_row is None:
                raise PersistenceError("PERSISTENCE_INTEGRITY_ERROR")
            run = PostgreSQLWorkflowMetadataRepository._run(run_row)
            if run.owner_user_id != worker_id or run.status not in (
                WorkflowMetadataStatus.RUNNING, WorkflowMetadataStatus.RESUMING,
            ):
                raise ConflictError("Investigation workflow fence is no longer active")
            updated_at = observed_at if observed_at > run.updated_at else run.updated_at + timedelta(microseconds=1)
            updated = connection.execute(
                """UPDATE workflow_runs SET status = ?, updated_at = ?
                   WHERE workflow_id = ? AND status IN (?, ?)""",
                (WorkflowMetadataStatus.COMPLETED.value, updated_at.isoformat(),
                 str(run.workflow_id), WorkflowMetadataStatus.RUNNING.value,
                 WorkflowMetadataStatus.RESUMING.value),
            )
            if updated.rowcount != 1:
                raise ConflictError("Investigation workflow fence is no longer active")
            completed = WorkflowRunMetadata.model_validate({
                **run.model_dump(), "status": WorkflowMetadataStatus.COMPLETED,
                "updated_at": updated_at,
            })
            PostgreSQLWorkflowMetadataRepository._append_run_event(
                connection, run=completed, kind="STATUS_CHANGED", previous_status=run.status,
            )
            result_id = uuid4()
            connection.execute(
                """INSERT INTO investigation_results
                   (result_id, job_id, workflow_id, execution_attempt_id, case_id,
                    status, result_json, result_hash, actions_executed, created_at)
                   VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?, ?, FALSE, ?)""",
                (str(result_id), str(job.id), str(job.workflow_id), str(attempt_id),
                 str(job.case_id), serialized, result_hash, updated_at.isoformat()),
            )
            return payload

    def get_by_job(self, job_id: UUID) -> dict | None:
        with self._persistence.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM investigation_results WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            return None if row is None else json.loads(row["result_json"])


class PostgreSQLHITLReviewService:
    """Deterministic PostgreSQL-owned HITL decision boundary.

    This service deliberately has no checkpoint, graph, provider, or action
    executor dependency.  The API layer authenticates the reviewer and checks
    case ownership/visibility; this service delegates the atomic, durable
    proposal transition to the PostgreSQL repository, where binding, audit,
    idempotency, and terminal-state checks share one transaction.
    """

    def __init__(self, repository: PostgreSQLProposalRepository) -> None:
        self._repository = repository

    def review(self, proposal_id: UUID, decision: ProposalStatus,
               review: HumanReview, *, workflow_id: UUID | None = None,
               review_id: UUID | None = None) -> ResolutionProposal:
        try:
            decision = ProposalStatus(decision)
        except (TypeError, ValueError):
            raise ConflictError("Review decision must be APPROVED or REJECTED") from None
        if decision not in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
            raise ConflictError("Review decision must be APPROVED or REJECTED")
        review = HumanReview.model_validate(review.model_dump())
        return self._repository.review(
            proposal_id, decision, review,
            workflow_id=workflow_id, review_id=review_id,
        )

class PostgreSQLWorkflowMetadataRepository:
    """Opt-in run metadata and idempotency store; contains no replayable checkpoint."""

    def __init__(self, persistence: PostgreSQLPersistence) -> None:
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


class PostgreSQLCheckpointSaver(SQLiteCheckpointSaver):
    """The custom authenticated LangGraph saver over PostgreSQL transactions."""
