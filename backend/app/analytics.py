"""Bounded, aggregate-only analytics over durable workflow data."""

from datetime import datetime
from typing import Any


class DurableAnalyticsRepository:
    """Database-portable aggregate reads; never returns customer or model data."""

    MAX_DURATION_ROWS = 10_000

    def __init__(self, persistence) -> None:
        self._persistence = persistence

    @staticmethod
    def _counts(rows, key: str = "bucket") -> dict[str, int]:
        return {str(row[key]): int(row["total"]) for row in rows}

    def summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        start_value, end_value = start.isoformat(), end.isoformat()
        with self._persistence.transaction() as connection:
            cases = connection.execute(
                """SELECT status AS bucket, count(*) AS total FROM support_cases
                   WHERE created_at >= ? AND created_at < ? GROUP BY status ORDER BY status""",
                (start_value, end_value),
            ).fetchall()
            runs = connection.execute(
                """SELECT w.workflow_id, w.case_id, w.status, w.provider, w.created_at, w.updated_at,
                          j.submission_mode
                     FROM workflow_runs w
                     LEFT JOIN investigation_jobs j ON j.workflow_id = w.workflow_id
                    WHERE w.created_at >= ? AND w.created_at < ?
                    ORDER BY w.created_at, w.workflow_id
                    LIMIT ?""",
                (start_value, end_value, self.MAX_DURATION_ROWS + 1),
            ).fetchall()
            run_count = connection.execute(
                """SELECT count(*) AS total FROM workflow_runs
                    WHERE created_at >= ? AND created_at < ?""",
                (start_value, end_value),
            ).fetchone()["total"]
            modes = connection.execute(
                """SELECT coalesce(j.submission_mode, 'UNAVAILABLE') AS bucket, count(*) AS total
                     FROM workflow_runs w LEFT JOIN investigation_jobs j ON j.workflow_id = w.workflow_id
                    WHERE w.created_at >= ? AND w.created_at < ?
                    GROUP BY coalesce(j.submission_mode, 'UNAVAILABLE') ORDER BY bucket""",
                (start_value, end_value),
            ).fetchall()
            statuses = connection.execute(
                """SELECT status AS bucket, count(*) AS total FROM workflow_runs
                    WHERE created_at >= ? AND created_at < ? GROUP BY status ORDER BY status""",
                (start_value, end_value),
            ).fetchall()
            providers = connection.execute(
                """SELECT provider AS bucket, count(*) AS total FROM workflow_runs
                    WHERE created_at >= ? AND created_at < ? GROUP BY provider ORDER BY provider""",
                (start_value, end_value),
            ).fetchall()
            decisions = connection.execute(
                """SELECT action AS bucket, count(*) AS total FROM case_resolution_decisions
                    WHERE decided_at >= ? AND decided_at < ? GROUP BY action ORDER BY action""",
                (start_value, end_value),
            ).fetchall()
            hitl = connection.execute(
                """SELECT count(*) AS total FROM workflow_audit_events a
                    JOIN workflow_runs w ON w.workflow_id = a.workflow_id
                   WHERE a.kind = 'HUMAN_REVIEW' AND a.created_at >= ? AND a.created_at < ?""",
                (start_value, end_value),
            ).fetchone()["total"]

        sampled = len(runs) > self.MAX_DURATION_ROWS
        duration_values = []
        if not sampled:
            for row in runs:
                try:
                    duration = (datetime.fromisoformat(row["updated_at"])
                                - datetime.fromisoformat(row["created_at"])).total_seconds() * 1000
                    if duration >= 0:
                        duration_values.append(duration)
                except (TypeError, ValueError):
                    continue
        return {
            "case_status_counts": self._counts(cases),
            "workflow_status_counts": self._counts(statuses),
            "mode_counts": self._counts(modes),
            "provider_counts": self._counts(providers),
            "decision_counts": self._counts(decisions),
            "workflow_count": int(run_count),
            "hitl_decision_count": int(hitl),
            "run_observations": tuple({
                "workflow_id": row["workflow_id"],
                "case_id": row["case_id"],
                "provider": row["provider"],
                "submission_mode": row["submission_mode"],
            } for row in runs[:self.MAX_DURATION_ROWS]),
            "duration_count": len(duration_values),
            "duration_min_ms": min(duration_values) if duration_values else None,
            "duration_avg_ms": (sum(duration_values) / len(duration_values)) if duration_values else None,
            "duration_max_ms": max(duration_values) if duration_values else None,
            "duration_measurement_available": bool(duration_values) and not sampled,
            "duration_measurement_sampled": sampled,
}
