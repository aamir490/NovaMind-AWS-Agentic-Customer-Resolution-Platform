"""Focused M5E2 durable checkpoint tests; application runtime remains in-memory."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.types import INTERRUPT
from langgraph.graph import END, START, StateGraph

from backend.app.agent import AgentRequest
from backend.app.graph_agent import GraphState
from backend.app.hitl import HITLState
from backend.app.langgraph_checkpoints import SQLiteCheckpointSaver
from backend.app.persistence import (
    PersistenceError,
    SQLiteCaseRepository,
    SQLitePersistence,
    SQLiteWorkflowMetadataRepository,
)
from backend.app.schemas import CaseCreate, CustomerCreate, OrderCreate
from backend.app.service import CaseService
from backend.app.workflow_metadata import WorkflowRunReservation


class SQLiteLangGraphCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "novamind.sqlite"
        store = SQLitePersistence(self.path, initialize=True)
        cases = CaseService(SQLiteCaseRepository(store))
        customer = cases.create_customer(CustomerCreate(name="Checkpoint owner"))
        order = cases.create_order(OrderCreate(
            customer_id=customer.id,
            items=[{"sku": "CHK-1", "name": "Checkpoint item", "quantity": 1}],
        ))
        self.case = cases.create_case(CaseCreate(
            customer_id=customer.id,
            order_id=order.id,
            subject="Durable graph state",
            description="Test checkpoint persistence without runtime recovery.",
        ))
        self.workflow_id = self._reserve_workflow(store)
        self.saver = SQLiteCheckpointSaver(store)

    def _reserve_workflow(self, store):
        workflow_id = uuid4()
        SQLiteWorkflowMetadataRepository(store).reserve(WorkflowRunReservation(
            workflow_id=workflow_id,
            run_id=uuid4(),
            case_id=self.case.id,
            owner_user_id=uuid4(),
            request_id=uuid4(),
            request_fingerprint=sha256(str(workflow_id).encode("ascii")).hexdigest(),
            provider="local_scripted",
        ))
        return workflow_id

    def _config(self, workflow_id=None, checkpoint_id=None):
        configurable = {"thread_id": str(workflow_id or self.workflow_id), "checkpoint_ns": ""}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    @staticmethod
    def _checkpoint(value, version="1"):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"state": value}
        checkpoint["channel_versions"] = {"state": version}
        checkpoint["versions_seen"] = {}
        return checkpoint

    def test_checkpoint_and_pending_writes_survive_saver_recreation(self):
        checkpoint = self._checkpoint({"phase": "review_required"})
        saved = self.saver.put(
            self._config(), checkpoint, {"source": "loop", "step": 1},
            {"state": "1"},
        )
        self.saver.put_writes(saved, (("review", {"decision": "pending"}),), "task-1")

        recreated = SQLiteCheckpointSaver(SQLitePersistence(self.path))
        restored = recreated.get_tuple(self._config())
        self.assertEqual(restored.checkpoint, checkpoint)
        self.assertEqual(restored.metadata["source"], "loop")
        self.assertEqual(restored.pending_writes, [
            ("task-1", "review", {"decision": "pending"}),
        ])

    def test_early_task_writes_are_atomically_attached_to_their_checkpoint(self):
        checkpoint = self._checkpoint({"phase": "scheduled"})
        early = self._config(checkpoint_id=checkpoint["id"])
        self.saver.put_writes(early, (("review", {"decision": "pending"}),), "task-early")

        saved = self.saver.put(
            self._config(), checkpoint, {"source": "loop", "step": 1}, {"state": "1"},
        )
        restored = self.saver.get_tuple(saved)
        self.assertEqual(restored.pending_writes, [
            ("task-early", "review", {"decision": "pending"}),
        ])
        with SQLitePersistence(self.path).transaction() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM langgraph_checkpoint_staged_writes"
            ).fetchone()[0], 0)

    def test_delayed_parent_writes_remain_valid_after_head_advances(self):
        parent = self.saver.put(
            self._config(), self._checkpoint({"step": 0}, "1"),
            {"source": "input", "step": -1}, {"state": "1"},
        )
        self.saver.put(
            parent, self._checkpoint({"step": 1}, "2"),
            {"source": "loop", "step": 0}, {"state": "2"},
        )

        delayed = (("state", {"step": 1}),)
        self.saver.put_writes(parent, delayed, "delayed-task")
        self.saver.put_writes(parent, delayed, "delayed-task")
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_CONFLICT"):
            self.saver.put_writes(
                parent, (("state", {"step": "forged"}),), "delayed-task",
            )

        restored_parent = self.saver.get_tuple(parent)
        self.assertEqual(restored_parent.pending_writes, [
            ("delayed-task", "state", {"step": 1}),
        ])

    def test_special_pending_write_retry_is_atomic_last_write_wins(self):
        checkpoint = self.saver.put(
            self._config(), self._checkpoint({"phase": "interrupt"}),
            {"source": "loop", "step": 0}, {"state": "1"},
        )
        self.saver.put_writes(
            checkpoint, ((INTERRUPT, ({"attempt": 1},)),), "retry-task",
        )
        self.saver.put_writes(
            checkpoint, ((INTERRUPT, ({"attempt": 2},)),), "retry-task",
        )

        restored = self.saver.get_tuple(checkpoint)
        self.assertEqual(restored.pending_writes, [
            ("retry-task", INTERRUPT, [{"attempt": 2}]),
        ])

    def test_workflow_isolation_and_scoped_listing_fail_closed(self):
        checkpoint = self._checkpoint({"workflow": str(self.workflow_id)})
        saved = self.saver.put(self._config(), checkpoint, {"step": 0}, {"state": "1"})
        other_workflow = self._reserve_workflow(SQLitePersistence(self.path))

        self.assertIsNone(self.saver.get_tuple(self._config(other_workflow, checkpoint["id"])))
        self.assertEqual(list(self.saver.list(self._config(other_workflow))), [])
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_SCOPE_REQUIRED"):
            list(self.saver.list(None))
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_SCOPE_MISMATCH"):
            self.saver.put_writes(
                self._config(other_workflow, saved["configurable"]["checkpoint_id"]),
                (("review", "forged"),),
                "task-2",
            )

    def test_stale_and_concurrent_checkpoint_writes_have_one_winner(self):
        root = self._checkpoint({"sequence": 0}, "1")
        parent = self.saver.put(self._config(), root, {"step": 0}, {"state": "1"})
        candidates = (
            self._checkpoint({"sequence": 1, "winner": "a"}, "2"),
            self._checkpoint({"sequence": 1, "winner": "b"}, "2"),
        )
        barrier = threading.Barrier(2)

        def write(candidate):
            saver = SQLiteCheckpointSaver(SQLitePersistence(self.path))
            barrier.wait(timeout=5)
            try:
                saver.put(parent, candidate, {"step": 1}, {"state": "2"})
                return candidate["id"]
            except PersistenceError as error:
                self.assertEqual(str(error), "CHECKPOINT_CONFLICT")
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, candidates))
        self.assertEqual(results.count("conflict"), 1)
        restored = self.saver.get_tuple(self._config())
        self.assertEqual(restored.checkpoint["id"], next(item for item in results if item != "conflict"))

        stale = self._checkpoint({"sequence": 2}, "3")
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_CONFLICT"):
            self.saver.put(parent, stale, {"step": 2}, {"state": "3"})

    def test_conflicting_duplicate_is_rejected_without_mutation(self):
        checkpoint = self._checkpoint({"phase": "original"})
        saved = self.saver.put(self._config(), checkpoint, {"step": 0}, {"state": "1"})
        forged = dict(checkpoint)
        forged["channel_values"] = {"state": {"phase": "forged"}}
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_CONFLICT"):
            self.saver.put(self._config(), forged, {"step": 0}, {"state": "1"})
        self.assertEqual(self.saver.get_tuple(saved).checkpoint, checkpoint)

    def test_immutable_rows_and_hash_validation_detect_corruption(self):
        checkpoint = self._checkpoint({"phase": "trusted"})
        self.saver.put(self._config(), checkpoint, {"step": 0}, {"state": "1"})
        store = SQLitePersistence(self.path)
        with self.assertRaises(PersistenceError):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE langgraph_checkpoints SET checkpoint_blob = ? WHERE thread_id = ?",
                    (b"forged", str(self.workflow_id)),
                )
        with store.transaction() as connection:
            connection.execute("DROP TRIGGER immutable_langgraph_checkpoint_blobs_update")
            connection.execute(
                """UPDATE langgraph_checkpoint_blobs SET value_blob = ?
                   WHERE thread_id = ? AND channel = 'state'""",
                (b"corrupt", str(self.workflow_id)),
            )
        with self.assertRaisesRegex(PersistenceError, "CHECKPOINT_INTEGRITY_ERROR"):
            self.saver.get_tuple(self._config())

    def test_failed_checkpoint_insert_rolls_back_channel_blobs(self):
        store = SQLitePersistence(self.path)
        with store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_m5e2_checkpoint
                   BEFORE INSERT ON langgraph_checkpoints
                   BEGIN SELECT RAISE(ABORT, 'forced checkpoint failure'); END"""
            )
        with self.assertRaises(PersistenceError):
            self.saver.put(
                self._config(), self._checkpoint({"phase": "rollback"}),
                {"step": 0}, {"state": "1"},
            )
        with store.transaction() as connection:
            checkpoint_count = connection.execute(
                "SELECT count(*) FROM langgraph_checkpoints WHERE thread_id = ?",
                (str(self.workflow_id),),
            ).fetchone()[0]
            blob_count = connection.execute(
                "SELECT count(*) FROM langgraph_checkpoint_blobs WHERE thread_id = ?",
                (str(self.workflow_id),),
            ).fetchone()[0]
        self.assertEqual((checkpoint_count, blob_count), (0, 0))

    def test_current_hitl_state_round_trips_through_a_compiled_graph(self):
        request = AgentRequest(case_id=self.case.id, message="Inspect this order")
        initial = {
            "workflow_id": str(self.workflow_id),
            "agent": GraphState(request=request).model_dump(mode="json"),
        }

        def compile_graph(saver):
            builder = StateGraph(HITLState)
            builder.add_node("mark", lambda state: {"error": "M5E2_COMPATIBLE"})
            builder.add_edge(START, "mark")
            builder.add_edge("mark", END)
            return builder.compile(checkpointer=saver)

        compile_graph(self.saver).invoke(initial, config=self._config())
        recreated = compile_graph(SQLiteCheckpointSaver(SQLitePersistence(self.path)))
        snapshot = recreated.get_state(self._config())
        restored = HITLState.model_validate(snapshot.values)
        self.assertEqual(restored.workflow_id, self.workflow_id)
        self.assertEqual(restored.agent.request, request)
        self.assertEqual(restored.error, "M5E2_COMPATIBLE")


if __name__ == "__main__":
    unittest.main()
