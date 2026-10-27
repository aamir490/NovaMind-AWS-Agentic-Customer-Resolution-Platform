"""Optional durable LangGraph checkpoints over a transactional UoW."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
import math
import random
import sqlite3
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .persistence import PersistenceError, UnitOfWork


_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_SERIALIZED_TYPES = frozenset({"null", "bytes", "bytearray", "msgpack"})


class SQLiteCheckpointSaver(BaseCheckpointSaver[str]):
    """Strict, workflow-scoped LangGraph saver backed by the shared UoW.

    A thread ID is the canonical workflow UUID and must already exist in
    ``workflow_runs``. Checkpoints are append-only; an insert must extend the
    current namespace head. Identical retries are idempotent, while forks,
    stale parents, and conflicting payloads are rejected.
    """

    def __init__(self, persistence: UnitOfWork) -> None:
        # No pickle and no dynamic imports outside LangGraph's built-in safe list.
        super().__init__(serde=JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        ))
        self._persistence = persistence

    @staticmethod
    def _canonical_uuid(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise PersistenceError("CHECKPOINT_INVALID")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError, TypeError):
            raise PersistenceError("CHECKPOINT_INVALID") from None
        canonical = str(parsed)
        if canonical != value:
            raise PersistenceError("CHECKPOINT_INVALID")
        return canonical

    @staticmethod
    def _text(value: Any, *, allow_empty: bool, maximum: int = 512) -> str:
        if not isinstance(value, str) or len(value) > maximum:
            raise PersistenceError("CHECKPOINT_INVALID")
        if not allow_empty and not value:
            raise PersistenceError("CHECKPOINT_INVALID")
        if any(ord(character) < 32 for character in value):
            raise PersistenceError("CHECKPOINT_INVALID")
        return value

    @classmethod
    def _scope(cls, config: RunnableConfig, *, checkpoint: bool = False) -> tuple[str, str, str | None]:
        if not isinstance(config, Mapping):
            raise PersistenceError("CHECKPOINT_INVALID")
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise PersistenceError("CHECKPOINT_INVALID")
        thread_id = cls._canonical_uuid(configurable.get("thread_id"), "thread_id")
        checkpoint_ns = cls._text(configurable.get("checkpoint_ns", ""), allow_empty=True)
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is not None:
            checkpoint_id = cls._canonical_uuid(checkpoint_id, "checkpoint_id")
        if checkpoint and checkpoint_id is None:
            raise PersistenceError("CHECKPOINT_INVALID")
        return thread_id, checkpoint_ns, checkpoint_id

    @staticmethod
    def _ensure_workflow(connection: sqlite3.Connection, thread_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM workflow_runs WHERE workflow_id = ?", (thread_id,),
        ).fetchone() is None:
            raise PersistenceError("CHECKPOINT_SCOPE_MISMATCH")

    @staticmethod
    def _digest(type_name: str, blob: bytes) -> str:
        return sha256(type_name.encode("ascii") + b"\x00" + blob).hexdigest()

    def _dump(self, value: Any) -> tuple[str, bytes, str]:
        try:
            type_name, blob = self.serde.dumps_typed(value)
        except Exception:
            raise PersistenceError("CHECKPOINT_INVALID") from None
        if type_name not in _SERIALIZED_TYPES or not isinstance(blob, bytes):
            raise PersistenceError("CHECKPOINT_INVALID")
        if len(blob) > _MAX_PAYLOAD_BYTES:
            raise PersistenceError("CHECKPOINT_INVALID")
        return type_name, blob, self._digest(type_name, blob)

    def _load(self, type_name: Any, blob: Any, digest: Any) -> Any:
        if type_name not in _SERIALIZED_TYPES or not isinstance(blob, bytes):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        if len(blob) > _MAX_PAYLOAD_BYTES or digest != self._digest(type_name, blob):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        try:
            return self.serde.loads_typed((type_name, blob))
        except Exception:
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR") from None

    @staticmethod
    def _version_key(version: Any) -> str:
        if isinstance(version, bool) or not isinstance(version, (str, int, float)):
            raise PersistenceError("CHECKPOINT_INVALID")
        if isinstance(version, float) and not math.isfinite(version):
            raise PersistenceError("CHECKPOINT_INVALID")
        return json.dumps([type(version).__name__, version], separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def _validate_checkpoint(cls, checkpoint: Any, expected_id: str | None = None) -> None:
        if not isinstance(checkpoint, dict):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        required = {"v", "id", "ts", "channel_values", "channel_versions", "versions_seen"}
        if not required.issubset(checkpoint):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        checkpoint_id = cls._canonical_uuid(checkpoint.get("id"), "checkpoint_id")
        if expected_id is not None and checkpoint_id != expected_id:
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        version = checkpoint.get("v")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        timestamp = checkpoint.get("ts")
        if not isinstance(timestamp, str):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        try:
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError
        except ValueError:
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR") from None
        if not isinstance(checkpoint.get("channel_values"), dict):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        versions = checkpoint.get("channel_versions")
        seen = checkpoint.get("versions_seen")
        if not isinstance(versions, dict) or not isinstance(seen, dict):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        for channel, version in versions.items():
            cls._text(channel, allow_empty=False)
            cls._version_key(version)
        for node, node_versions in seen.items():
            if not isinstance(node, str) or not isinstance(node_versions, dict):
                raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
            for channel, channel_version in node_versions.items():
                cls._text(channel, allow_empty=False)
                cls._version_key(channel_version)

    @staticmethod
    def _config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
        return {"configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }}

    def _tuple_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> CheckpointTuple:
        thread_id = row["thread_id"]
        checkpoint_ns = row["checkpoint_ns"]
        checkpoint_id = row["checkpoint_id"]
        checkpoint = self._load(
            row["checkpoint_type"], row["checkpoint_blob"], row["checkpoint_hash"],
        )
        metadata = self._load(row["metadata_type"], row["metadata_blob"], row["metadata_hash"])
        if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
        checkpoint = dict(checkpoint)
        checkpoint["channel_values"] = {}
        try:
            self._validate_checkpoint(checkpoint, checkpoint_id)
        except PersistenceError:
            raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR") from None
        for channel, version in checkpoint["channel_versions"].items():
            blob_row = connection.execute(
                """SELECT value_type, value_blob, value_hash
                   FROM langgraph_checkpoint_blobs
                   WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version_key = ?""",
                (thread_id, checkpoint_ns, channel, self._version_key(version)),
            ).fetchone()
            if blob_row is None:
                raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
            if blob_row["value_type"] == "empty":
                if blob_row["value_blob"] != b"" or blob_row["value_hash"] != self._digest("empty", b""):
                    raise PersistenceError("CHECKPOINT_INTEGRITY_ERROR")
                continue
            checkpoint["channel_values"][channel] = self._load(
                blob_row["value_type"], blob_row["value_blob"], blob_row["value_hash"],
            )
        pending_writes = []
        for write in connection.execute(
            """SELECT task_id, channel, value_type, value_blob, value_hash
               FROM langgraph_checkpoint_writes
               WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
               ORDER BY task_path, task_id, write_index""",
            (thread_id, checkpoint_ns, checkpoint_id),
        ):
            pending_writes.append((
                write["task_id"], write["channel"],
                self._load(write["value_type"], write["value_blob"], write["value_hash"]),
            ))
        parent_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config=self._config(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(self._config(thread_id, checkpoint_ns, parent_id) if parent_id else None),
            pending_writes=pending_writes,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._scope(config)
        with self._persistence.transaction() as connection:
            self._ensure_workflow(connection, thread_id)
            if checkpoint_id is None:
                row = connection.execute(
                    """SELECT * FROM langgraph_checkpoints
                       WHERE thread_id = ? AND checkpoint_ns = ?
                       ORDER BY checkpoint_id DESC LIMIT 1""",
                    (thread_id, checkpoint_ns),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM langgraph_checkpoints
                       WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?""",
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            return None if row is None else self._tuple_from_row(connection, row)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            raise PersistenceError("CHECKPOINT_SCOPE_REQUIRED")
        thread_id, checkpoint_ns, checkpoint_id = self._scope(config)
        configurable = config["configurable"]
        namespace_supplied = "checkpoint_ns" in configurable
        before_id = None
        if before is not None:
            before_thread, before_ns, before_id = self._scope(before, checkpoint=True)
            if before_thread != thread_id or (namespace_supplied and before_ns != checkpoint_ns):
                raise PersistenceError("CHECKPOINT_SCOPE_MISMATCH")
        if limit is not None and limit <= 0:
            return iter(())
        with self._persistence.transaction() as connection:
            self._ensure_workflow(connection, thread_id)
            sql = "SELECT * FROM langgraph_checkpoints WHERE thread_id = ?"
            parameters: list[Any] = [thread_id]
            if namespace_supplied:
                sql += " AND checkpoint_ns = ?"
                parameters.append(checkpoint_ns)
            if checkpoint_id is not None:
                sql += " AND checkpoint_id = ?"
                parameters.append(checkpoint_id)
            if before_id is not None:
                sql += " AND checkpoint_id < ?"
                parameters.append(before_id)
            sql += " ORDER BY checkpoint_id DESC"
            rows = connection.execute(sql, parameters).fetchall()
            results: list[CheckpointTuple] = []
            for row in rows:
                item = self._tuple_from_row(connection, row)
                if filter and not all(item.metadata.get(key) == value for key, value in filter.items()):
                    continue
                results.append(item)
                if limit is not None and len(results) >= limit:
                    break
        return iter(results)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, parent_id = self._scope(config)
        full_candidate = dict(checkpoint)
        self._validate_checkpoint(full_candidate)
        checkpoint_id = full_candidate["id"]
        candidate = dict(full_candidate)
        values = candidate.pop("channel_values")
        if not set(values).issubset(candidate["channel_versions"]):
            raise PersistenceError("CHECKPOINT_INVALID")
        if not isinstance(new_versions, dict):
            raise PersistenceError("CHECKPOINT_INVALID")
        for channel, version in new_versions.items():
            self._text(channel, allow_empty=False)
            if candidate["channel_versions"].get(channel) != version:
                raise PersistenceError("CHECKPOINT_INVALID")
        checkpoint_serialized = self._dump(candidate)
        normalized_metadata = get_checkpoint_metadata(config, metadata)
        if not isinstance(normalized_metadata, dict) or any(
            not isinstance(key, str) for key in normalized_metadata
        ):
            raise PersistenceError("CHECKPOINT_INVALID")
        metadata_serialized = self._dump(normalized_metadata)
        serialized_blobs: list[tuple[str, str, str, bytes, str]] = []
        for channel, version in new_versions.items():
            if channel in values:
                value_type, value_blob, value_hash = self._dump(values[channel])
            else:
                value_type, value_blob = "empty", b""
                value_hash = self._digest(value_type, value_blob)
            serialized_blobs.append((channel, self._version_key(version), value_type, value_blob, value_hash))

        with self._persistence.transaction() as connection:
            self._ensure_workflow(connection, thread_id)
            existing = connection.execute(
                """SELECT * FROM langgraph_checkpoints
                   WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?""",
                (thread_id, checkpoint_ns, checkpoint_id),
            ).fetchone()
            if existing is not None:
                expected = (
                    parent_id, checkpoint_serialized[0], checkpoint_serialized[1], checkpoint_serialized[2],
                    metadata_serialized[0], metadata_serialized[1], metadata_serialized[2],
                )
                actual = (
                    existing["parent_checkpoint_id"], existing["checkpoint_type"],
                    existing["checkpoint_blob"], existing["checkpoint_hash"],
                    existing["metadata_type"], existing["metadata_blob"], existing["metadata_hash"],
                )
                if actual != expected:
                    raise PersistenceError("CHECKPOINT_CONFLICT")
                stored = self._tuple_from_row(connection, existing)
                if stored.checkpoint != full_candidate or stored.metadata != normalized_metadata:
                    raise PersistenceError("CHECKPOINT_CONFLICT")
                return self._config(thread_id, checkpoint_ns, checkpoint_id)

            head = connection.execute(
                """SELECT checkpoint_id FROM langgraph_checkpoints
                   WHERE thread_id = ? AND checkpoint_ns = ?
                   ORDER BY checkpoint_id DESC LIMIT 1""",
                (thread_id, checkpoint_ns),
            ).fetchone()
            head_id = head["checkpoint_id"] if head else None
            if parent_id != head_id or (head_id is not None and checkpoint_id <= head_id):
                raise PersistenceError("CHECKPOINT_CONFLICT")
            for channel, version_key, value_type, value_blob, value_hash in serialized_blobs:
                prior = connection.execute(
                    """SELECT value_type, value_blob, value_hash FROM langgraph_checkpoint_blobs
                       WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version_key = ?""",
                    (thread_id, checkpoint_ns, channel, version_key),
                ).fetchone()
                if prior is not None:
                    if tuple(prior) != (value_type, value_blob, value_hash):
                        raise PersistenceError("CHECKPOINT_CONFLICT")
                    continue
                connection.execute(
                    """INSERT INTO langgraph_checkpoint_blobs
                       (thread_id, checkpoint_ns, channel, version_key, value_type, value_blob, value_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, checkpoint_ns, channel, version_key, value_type, value_blob, value_hash),
                )
            for channel, version in candidate["channel_versions"].items():
                if connection.execute(
                    """SELECT 1 FROM langgraph_checkpoint_blobs
                       WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version_key = ?""",
                    (thread_id, checkpoint_ns, channel, self._version_key(version)),
                ).fetchone() is None:
                    raise PersistenceError("CHECKPOINT_INVALID")
            try:
                connection.execute(
                    """INSERT INTO langgraph_checkpoints
                       (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                        checkpoint_type, checkpoint_blob, checkpoint_hash,
                        metadata_type, metadata_blob, metadata_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, checkpoint_ns, checkpoint_id, parent_id,
                     *checkpoint_serialized, *metadata_serialized, candidate["ts"]),
                )
            except sqlite3.IntegrityError:
                raise PersistenceError("CHECKPOINT_CONFLICT") from None
            # LangGraph can submit a task write just before its corresponding
            # checkpoint reaches the saver.  Stage those writes with the
            # workflow scope, then attach them in the same transaction that
            # creates the checkpoint.  They are never visible as pending
            # writes for another checkpoint or workflow.
            staged = connection.execute(
                """SELECT task_id, write_index, channel, value_type, value_blob,
                          value_hash, task_path
                   FROM langgraph_checkpoint_staged_writes
                   WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                   ORDER BY task_path, task_id, write_index""",
                (thread_id, checkpoint_ns, checkpoint_id),
            ).fetchall()
            for row in staged:
                connection.execute(
                    """INSERT INTO langgraph_checkpoint_writes
                       (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index,
                        channel, value_type, value_blob, value_hash, task_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, checkpoint_ns, checkpoint_id, row["task_id"],
                     row["write_index"], row["channel"], row["value_type"],
                     row["value_blob"], row["value_hash"], row["task_path"]),
                )
            if staged:
                connection.execute(
                    """DELETE FROM langgraph_checkpoint_staged_writes
                       WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?""",
                    (thread_id, checkpoint_ns, checkpoint_id),
                )
        return self._config(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._scope(config, checkpoint=True)
        task_id = self._text(task_id, allow_empty=False)
        task_path = self._text(task_path, allow_empty=True, maximum=2048)
        serialized = []
        for index, write in enumerate(writes):
            if not isinstance(write, tuple) or len(write) != 2:
                raise PersistenceError("CHECKPOINT_INVALID")
            channel = self._text(write[0], allow_empty=False)
            value_type, value_blob, value_hash = self._dump(write[1])
            serialized.append((WRITES_IDX_MAP.get(channel, index), channel, value_type, value_blob, value_hash))
        with self._persistence.transaction() as connection:
            self._ensure_workflow(connection, thread_id)
            exists = connection.execute(
                """SELECT 1 FROM langgraph_checkpoints
                   WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?""",
                (thread_id, checkpoint_ns, checkpoint_id),
            ).fetchone()
            staged = exists is None
            if staged and connection.execute(
                """SELECT 1 FROM langgraph_checkpoints
                   WHERE checkpoint_ns = ? AND checkpoint_id = ?""",
                (checkpoint_ns, checkpoint_id),
            ).fetchone() is not None:
                raise PersistenceError("CHECKPOINT_SCOPE_MISMATCH")
            if not staged:
                # Delayed parent writes are valid, but a write can never target
                # an abandoned branch.
                on_current_lineage = connection.execute(
                    """WITH RECURSIVE lineage(checkpoint_id, parent_checkpoint_id) AS (
                       SELECT checkpoint_id, parent_checkpoint_id
                       FROM langgraph_checkpoints
                       WHERE thread_id = ? AND checkpoint_ns = ?
                         AND checkpoint_id = (
                           SELECT max(checkpoint_id) FROM langgraph_checkpoints
                           WHERE thread_id = ? AND checkpoint_ns = ?
                         )
                       UNION ALL
                       SELECT parent.checkpoint_id, parent.parent_checkpoint_id
                       FROM langgraph_checkpoints parent
                       JOIN lineage child
                         ON parent.thread_id = ? AND parent.checkpoint_ns = ?
                        AND parent.checkpoint_id = child.parent_checkpoint_id
                   )
                   SELECT 1 FROM lineage WHERE checkpoint_id = ?""",
                    (thread_id, checkpoint_ns, thread_id, checkpoint_ns,
                     thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
                if on_current_lineage is None:
                    raise PersistenceError("CHECKPOINT_CONFLICT")
            writes_table = (
                "langgraph_checkpoint_staged_writes" if staged
                else "langgraph_checkpoint_writes"
            )
            for write_index, channel, value_type, value_blob, value_hash in serialized:
                existing = connection.execute(
                    f"""SELECT channel, value_type, value_blob, value_hash, task_path
                       FROM {writes_table}
                       WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                         AND task_id = ? AND write_index = ?""",
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index),
                ).fetchone()
                expected = (channel, value_type, value_blob, value_hash, task_path)
                if existing is not None:
                    if tuple(existing) == expected:
                        continue
                    if write_index >= 0 or existing["channel"] != channel:
                        raise PersistenceError("CHECKPOINT_CONFLICT")
                    # Negative indexes are LangGraph's special ERROR/SCHEDULED/
                    # INTERRUPT/RESUME slots. Their contract is last-write-wins.
                    connection.execute(
                        f"""DELETE FROM {writes_table}
                           WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                             AND task_id = ? AND write_index = ?""",
                        (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index),
                    )
                connection.execute(
                    f"""INSERT INTO {writes_table}
                       (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index,
                        channel, value_type, value_blob, value_hash, task_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index,
                     channel, value_type, value_blob, value_hash, task_path),
                )

    def delete_thread(self, thread_id: str) -> None:
        thread_id = self._canonical_uuid(thread_id, "thread_id")
        with self._persistence.transaction() as connection:
            self._ensure_workflow(connection, thread_id)
            connection.execute("DELETE FROM langgraph_checkpoint_writes WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM langgraph_checkpoint_staged_writes WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM langgraph_checkpoints WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM langgraph_checkpoint_blobs WHERE thread_id = ?", (thread_id,))

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_version = 0
        elif isinstance(current, int):
            current_version = current
        else:
            current_version = int(current.split(".")[0])
        return f"{current_version + 1:032}.{random.random():016}"
