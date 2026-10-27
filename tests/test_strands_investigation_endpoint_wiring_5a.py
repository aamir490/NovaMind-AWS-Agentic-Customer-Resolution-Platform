"""Offline Task 5A coverage for PostgreSQL-backed Strands endpoint wiring."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import os
import re
import unittest
from threading import Barrier, Event
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from backend.app.agent import AgentResult
from backend.app.domain import CaseStatus, InvestigationJobStatus, InvestigationMode
from backend.app.frontend_routes import case_investigation, start_case_investigation
from backend.app.hitl import WorkflowResult
from backend.app.main import create_app
from backend.app.customer_case_routes import create_my_case
from backend.app.schemas import CaseCreate, CustomerCaseCreate, CustomerCreate, OrderCreate
from backend.app.security import (
    AuthenticatedIdentity, LocalAuthenticationProvider, LocalCredential, SecurityError,
    authenticated,
)
from backend.app.service import ConflictError
from backend.app.strands_worker import PostgreSQLStrandsWorker
from backend.app.workflow_metadata import WorkflowMetadataStatus


_EXPECTED_DATABASE = "novamind_phase_c_test_20261026"
_DISPOSABLE_DATABASE = re.compile(r"novamind_phase_c_test_20261026$")


class _Control:
    def get(self):
        return SimpleNamespace(mode=InvestigationMode.MANUAL)


class _Jobs:
    def __init__(self, job, *, created=True):
        self.job = job
        self.created = created
        self.starts = []

    def get_by_case(self, case_id):
        if case_id != self.job.case_id:
            raise RuntimeError("unexpected case")
        return self.job

    def register_manual_start(self, job_id, *, admin_user_id, idempotency_key):
        self.starts.append((job_id, admin_user_id, idempotency_key))
        return self.job, self.created


class _Executor:
    def __init__(self):
        self.submissions = []

    def submit_as(self, job_id, *, worker_id):
        self.submissions.append((job_id, worker_id))
        return True


class StrandsInvestigationEndpointWiringTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(agent_engine="strands", strands_model_factory=lambda: object())
        cases = self.app.state.case_service
        customer = cases.create_customer(CustomerCreate(name="Task 5A customer"))
        order = cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "TASK5A-1", "name": "Task 5A item", "quantity": 1}],
        ))
        self.case = cases.create_case(CaseCreate(
            customer_id=customer.id, order_id=order.id,
            subject="Task 5A investigation", description="Durable Strands endpoint test",
        ))
        self.identity = AuthenticatedIdentity(user_id=uuid4(), role="ADMIN")
        self.customer_identity = AuthenticatedIdentity(
            user_id=uuid4(), role="CUSTOMER", customer_id=self.case.customer_id,
        )
        token = "task-5a-admin-token"
        customer_token = "task-5a-customer-token"
        self.app.state.auth_provider = LocalAuthenticationProvider((
            LocalCredential(token=token, identity=self.identity),
            LocalCredential(token=customer_token, identity=self.customer_identity),
        ))
        self.token = token
        self.customer_token = customer_token

    def tearDown(self):
        self.app.state.frontend_runtime.close()

    def _job(self, *, status=InvestigationJobStatus.PENDING, workflow_id=None,
             execution_attempt_id=None):
        timestamp = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=uuid4(), case_id=self.case.id, status=status, workflow_id=workflow_id,
            execution_attempt_id=execution_attempt_id, submission_mode=InvestigationMode.MANUAL,
            mode_version=1, created_at=timestamp,
        )

    def _request(self):
        return SimpleNamespace(app=self.app)

    def test_start_queues_authorized_job_without_inline_execution(self):
        job = self._job()
        jobs = _Jobs(job)
        executor = _Executor()
        self.app.state.investigation_jobs = jobs
        self.app.state.investigation_executor = executor
        self.app.state.investigation_workflow_metadata = None
        self.app.state.investigation_control = _Control()
        key = uuid4()
        with authenticated(self.app.state.auth_provider, self.token):
            snapshot = start_case_investigation(self.case.id, self._request(), key)
        self.assertEqual(snapshot.state, "PENDING")
        self.assertEqual(executor.submissions, [(job.id, self.identity.user_id)])
        self.assertEqual(jobs.starts, [(job.id, self.identity.user_id, key)])

    def test_customer_cannot_start_staff_investigation(self):
        self.app.state.investigation_jobs = _Jobs(self._job())
        self.app.state.investigation_executor = _Executor()
        with authenticated(self.app.state.auth_provider, self.customer_token):
            with self.assertRaises(SecurityError):
                start_case_investigation(self.case.id, self._request(), uuid4())

    def test_idempotent_duplicate_start_is_not_queued_again(self):
        job = self._job()
        jobs = _Jobs(job, created=False)
        executor = _Executor()
        self.app.state.investigation_jobs = jobs
        self.app.state.investigation_executor = executor
        self.app.state.investigation_workflow_metadata = None
        self.app.state.investigation_control = _Control()
        with authenticated(self.app.state.auth_provider, self.token):
            snapshot = start_case_investigation(self.case.id, self._request(), uuid4())
        self.assertEqual(snapshot.state, "PENDING")
        self.assertEqual(executor.submissions, [])

    def test_status_projects_durable_strands_result_without_checkpoint(self):
        workflow_id = uuid4()
        job = self._job(
            status=InvestigationJobStatus.CLAIMED,
            workflow_id=workflow_id,
            execution_attempt_id=uuid4(),
        )
        result = WorkflowResult(
            workflow_id=workflow_id, status="COMPLETED",
            message="Informational investigation complete.",
            agent_result=AgentResult(
                status="INFORMATIONAL", customer_response="Informational investigation complete.",
                steps_used=1, history=(), actions_executed=False,
            ),
            actions_executed=False,
        )
        self.app.state.investigation_jobs = _Jobs(job)
        self.app.state.investigation_control = _Control()
        self.app.state.investigation_workflow_metadata = SimpleNamespace(
            get=lambda requested: SimpleNamespace(
                workflow_id=requested, case_id=self.case.id, provider="bedrock",
                status=WorkflowMetadataStatus.COMPLETED, failure_category=None,
                failure_stage=None, updated_at=datetime.now(timezone.utc),
            )
        )
        self.app.state.investigation_results = SimpleNamespace(
            get_by_job=lambda requested: result.model_dump(mode="json")
        )
        with authenticated(self.app.state.auth_provider, self.token):
            snapshot = case_investigation(self.case.id, self._request())
        self.assertEqual(snapshot.state, "COMPLETED")
        self.assertEqual(snapshot.workflow_status, WorkflowMetadataStatus.COMPLETED)
        self.assertEqual(snapshot.message, result.message)
        self.assertFalse(snapshot.actions_executed)

    def test_worker_submit_as_is_asynchronous_boundary(self):
        worker = PostgreSQLStrandsWorker(
            object(), object(), tools=object(), model_factory=lambda: object(),
        )
        finished = Event()
        calls = []

        def run_once(job_id, *, worker_id=None):
            calls.append((job_id, worker_id))
            finished.set()

        worker.run_once = run_once
        worker_id = uuid4()
        job_id = uuid4()
        try:
            self.assertTrue(worker.submit_as(job_id, worker_id=worker_id))
            self.assertTrue(finished.wait(2))
            self.assertEqual(calls, [(job_id, worker_id)])
        finally:
            worker.close()


@unittest.skipUnless(
    os.environ.get("NOVAMIND_POSTGRES_TEST_DSN"),
    "NOVAMIND_POSTGRES_TEST_DSN is not configured",
)
class PostgreSQLStrandsInvestigationEndpointIntegrationTests(unittest.TestCase):
    """Real PostgreSQL endpoint wiring with mocked Strands execution only."""

    @classmethod
    def setUpClass(cls):
        dsn = os.environ["NOVAMIND_POSTGRES_TEST_DSN"]
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname
                or not _DISPOSABLE_DATABASE.fullmatch(database)):
            raise RuntimeError(
                "NOVAMIND_POSTGRES_TEST_DSN must target exactly "
                f"{_EXPECTED_DATABASE}"
            )
        cls.dsn = dsn
        from psycopg import connect
        connection = connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != _EXPECTED_DATABASE:
                    raise RuntimeError("PostgreSQL database identity mismatch")
        finally:
            connection.close()

    @classmethod
    def _reset_schema(cls):
        from psycopg import connect
        connection = connect(cls.dsn, autocommit=True, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        finally:
            connection.close()

    def setUp(self):
        self._reset_schema()
        self.model_factory = Mock()
        self.app = create_app(
            auth_provider=None, postgres_dsn=self.dsn, agent_engine="strands",
            strands_model_factory=self.model_factory,
        )
        cases = self.app.state.case_service
        self.customer = cases.create_customer(CustomerCreate(name="Task 5A PG customer"))
        self.other_customer = cases.create_customer(CustomerCreate(name="Task 5A other customer"))
        self.order = cases.create_order(OrderCreate(
            customer_id=self.customer.id,
            items=[{"sku": "TASK5A-PG-1", "name": "Task 5A PostgreSQL item", "quantity": 1}],
        ))
        self.other_order = cases.create_order(OrderCreate(
            customer_id=self.other_customer.id,
            items=[{"sku": "TASK5A-PG-2", "name": "Task 5A other item", "quantity": 1}],
        ))
        self.identities = {
            "customer": AuthenticatedIdentity(
                user_id=uuid4(), role="CUSTOMER", customer_id=self.customer.id,
            ),
            "other_customer": AuthenticatedIdentity(
                user_id=uuid4(), role="CUSTOMER", customer_id=self.other_customer.id,
            ),
            "admin": AuthenticatedIdentity(user_id=uuid4(), role="ADMIN"),
            "reviewer": AuthenticatedIdentity(user_id=uuid4(), role="REVIEWER"),
        }
        self.tokens = {name: f"task-5a-pg-{name}-token" for name in self.identities}
        self.provider = LocalAuthenticationProvider(tuple(
            LocalCredential(token=self.tokens[name], identity=identity)
            for name, identity in self.identities.items()
        ))
        self.app.state.auth_provider = self.provider

    def tearDown(self):
        executor = getattr(self.app.state, "investigation_executor", None)
        if executor is not None:
            executor.close()
        self._reset_schema()

    def _request(self, app=None):
        return SimpleNamespace(app=app or self.app)

    def _case_body(self):
        return CustomerCaseCreate(
            order_id=self.order.id, subject="Task 5A PG case",
            description="A durable PostgreSQL Strands endpoint case.",
        )

    def _completed_result(self, workflow_id):
        message = "Mocked Bedrock investigation complete."
        return WorkflowResult(
            workflow_id=workflow_id, status="COMPLETED", message=message,
            agent_result=AgentResult(
                status="INFORMATIONAL", customer_response=message,
                steps_used=1, history=(), actions_executed=False,
            ),
            actions_executed=False,
        )

    def test_start_is_authorized_idempotent_and_does_not_execute_inline(self):
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            first = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
            replay = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        self.assertEqual(replay.id, first.id)
        job = self.app.state.investigation_jobs.get_by_case(first.id)
        self.assertEqual(job.status, InvestigationJobStatus.PENDING)

        with authenticated(self.provider, self.tokens["other_customer"]):
            with self.assertRaises(SecurityError):
                create_my_case(
                    CustomerCaseCreate(
                        order_id=self.order.id, subject="Cross-customer",
                        description="Must be denied.",
                    ), self._request(), self.app.state.case_service, uuid4(),
                )

        with patch.object(
            self.app.state.investigation_executor, "submit_as", return_value=True,
        ) as submit:
            with authenticated(self.provider, self.tokens["admin"]):
                snapshot = start_case_investigation(
                    first.id, self._request(), uuid4(),
                )
        self.assertEqual(snapshot.state, "PENDING")
        submit.assert_called_once_with(job.id, worker_id=self.identities["admin"].user_id)
        self.model_factory.assert_not_called()

    def test_mocked_strands_result_survives_app_recreation_without_langgraph(self):
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            case = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        job = self.app.state.investigation_jobs.get_by_case(case.id)
        result = self._completed_result(job.id)
        with patch("backend.app.strands_worker.StrandsWorkflow") as workflow:
            workflow.return_value.run_durable.return_value = result
            completed = self.app.state.investigation_executor.run_once(
                job.id, worker_id=self.identities["admin"].user_id,
            )
        self.assertEqual(completed.status, "COMPLETED")
        self.assertFalse(completed.actions_executed)

        restarted = create_app(
            auth_provider=self.provider, postgres_dsn=self.dsn,
            agent_engine="strands", strands_model_factory=self.model_factory,
        )
        try:
            with authenticated(self.provider, self.tokens["admin"]):
                snapshot = case_investigation(case.id, self._request(restarted))
            self.assertEqual(snapshot.state, "COMPLETED")
            self.assertEqual(snapshot.workflow_status, WorkflowMetadataStatus.COMPLETED)
            self.assertEqual(snapshot.message, result.message)
            self.assertFalse(snapshot.actions_executed)
            self.assertIsNone(restarted.state.investigation_dispatcher)
            self.assertIsNone(restarted.state.frontend_runtime._checkpointer)
            with restarted.state.persistence.transaction() as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) AS total FROM langgraph_checkpoints"
                ).fetchone()["total"], 0)
        finally:
            restarted.state.investigation_executor.close()

    def test_simultaneous_duplicate_starts_have_one_queue_submission(self):
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            case = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        executor = Mock()
        executor.submit_as.return_value = True
        self.app.state.investigation_executor = executor
        barrier = Barrier(2)

        def start_once():
            barrier.wait(timeout=5)
            with authenticated(self.provider, self.tokens["admin"]):
                return start_case_investigation(case.id, self._request(), key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshots = list(pool.map(lambda _: start_once(), (1, 2)))
        self.assertEqual([snapshot.state for snapshot in snapshots], ["PENDING", "PENDING"])
        executor.submit_as.assert_called_once()

    def test_competing_claims_have_one_winner_for_one_strands_job(self):
        from backend.app.postgres_persistence import (
            PostgreSQLInvestigationJobRepository, PostgreSQLPersistence,
        )
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            case = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        job = self.app.state.investigation_jobs.get_by_case(case.id)
        barrier = Barrier(2)
        workers = (uuid4(), uuid4())

        def claim(worker_id):
            barrier.wait(timeout=5)
            repository = PostgreSQLInvestigationJobRepository(PostgreSQLPersistence(self.dsn))
            return repository.claim(job.id, worker_id=worker_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, workers))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_interrupted_claim_is_recovery_required_and_fenced_after_restart(self):
        from backend.app.postgres_persistence import (
            PostgreSQLInvestigationJobRepository, PostgreSQLPersistence,
        )
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            case = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        repository = PostgreSQLInvestigationJobRepository(self.app.state.persistence)
        job = repository.get_by_case(case.id)
        worker_id, replacement_worker = uuid4(), uuid4()
        claim = repository.claim(job.id, worker_id=worker_id, lease_seconds=60)
        self.assertIsNotNone(claim)
        repository.bind_claim(
            job.id, worker_id=worker_id, claim_id=claim.claim_id, provider="bedrock",
        )
        repository.mark_execution_attempt(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
        )
        repository.require_recovery(
            job.id, worker_id=worker_id, claim_id=claim.claim_id,
        )
        restarted = PostgreSQLInvestigationJobRepository(PostgreSQLPersistence(self.dsn))
        recovered = restarted.get_by_case(case.id)
        self.assertEqual(recovered.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertIsNone(restarted.claim(job.id, worker_id=replacement_worker))

    def test_expired_strands_lease_is_fenced_before_binding(self):
        from backend.app.postgres_persistence import (
            PostgreSQLInvestigationJobRepository, PostgreSQLPersistence,
        )
        key = uuid4()
        with authenticated(self.provider, self.tokens["customer"]):
            case = create_my_case(
                self._case_body(), self._request(), self.app.state.case_service, key,
            )
        repository = PostgreSQLInvestigationJobRepository(self.app.state.persistence)
        job = repository.get_by_case(case.id)
        worker_id = uuid4()
        claimed_at = datetime.now(timezone.utc)
        claim = repository.claim(
            job.id, worker_id=worker_id, lease_seconds=1, now=claimed_at,
        )
        self.assertIsNotNone(claim)
        with self.assertRaises(ConflictError):
            repository.bind_claim(
                job.id, worker_id=worker_id, claim_id=claim.claim_id,
                provider="bedrock", now=claimed_at + timedelta(seconds=2),
            )
        restarted = PostgreSQLInvestigationJobRepository(PostgreSQLPersistence(self.dsn))
        recovered = restarted.get_by_case(case.id)
        self.assertEqual(recovered.status, InvestigationJobStatus.RECOVERY_REQUIRED)
        self.assertIsNone(recovered.workflow_id)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
