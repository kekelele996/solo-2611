"""Dispatch throughput, stage medians, and capacity timeline."""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from solo_auto.store.repo import Repository


@dataclass(slots=True)
class Outcome:
    task_id: str
    finished_at: str
    outcome: str
    queued_s: float = 0.0
    authoring_s: float = 0.0
    wait_container_s: float = 0.0
    running_s: float = 0.0
    delivering_s: float = 0.0
    recording_s: float = 0.0
    exporting_s: float = 0.0
    total_s: float = 0.0
    attempts: int = 0
    failure_kind: str = ""
    project_id: str = ""


class ThroughputLedger:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def record(self, outcome: Outcome) -> None:
        values = asdict(outcome)
        self.repo.execute(
            """
            INSERT INTO dispatch_outcomes(
              task_id, finished_at, outcome, queued_s, authoring_s, wait_container_s,
              running_s, delivering_s, recording_s, exporting_s, total_s, attempts,
              failure_kind, project_id
            ) VALUES(
              :task_id, :finished_at, :outcome, :queued_s, :authoring_s, :wait_container_s,
              :running_s, :delivering_s, :recording_s, :exporting_s, :total_s, :attempts,
              :failure_kind, :project_id
            )
            """,
            values,
        )

    def sample_capacity(
        self,
        *,
        occupied: int,
        limit_effective: int,
        limit_floor: int,
        in_hand: int,
        sessions: int,
        held: list[str],
        waiting: int = 0,
    ) -> None:
        minute = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        self.repo.execute(
            """
            INSERT INTO dispatch_capacity(
              at, occupied, limit_effective, limit_floor, in_hand, sessions, waiting, held
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(at) DO UPDATE SET
              occupied=excluded.occupied, limit_effective=excluded.limit_effective,
              limit_floor=excluded.limit_floor, in_hand=excluded.in_hand,
              sessions=excluded.sessions, waiting=excluded.waiting, held=excluded.held
            """,
            (
                minute,
                occupied,
                limit_effective,
                limit_floor,
                in_hand,
                sessions,
                waiting,
                json.dumps(held, ensure_ascii=False),
            ),
        )
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        self.repo.execute("DELETE FROM dispatch_capacity WHERE at < ?", (cutoff,))

    def summary(self, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()
        rows = self.repo.query(
            "SELECT * FROM dispatch_outcomes WHERE finished_at >= ? ORDER BY finished_at",
            (cutoff,),
        )
        completed = sum(1 for row in rows if row["outcome"] in {"done", "awaiting_audit"})
        failed = sum(1 for row in rows if row["outcome"] in {"failed", "quarantined"})
        quarantined = sum(1 for row in rows if row["outcome"] == "quarantined")
        fields = ("authoring_s", "running_s", "delivering_s", "recording_s", "exporting_s")
        medians = {
            key: round(statistics.median([float(row[key] or 0) for row in rows]), 1) if rows else 0.0
            for key in fields
        }
        utilization = self._utilization(hours)
        return {
            "hours": hours,
            "completed": completed,
            "failed": failed,
            "quarantined": quarantined,
            "per_hour": round(completed / hours, 2) if hours else 0,
            "medians_seconds": medians,
            "container_utilization": utilization,
        }

    def capacity(self, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()
        rows = self.repo.query("SELECT * FROM dispatch_capacity WHERE at >= ? ORDER BY at", (cutoff,))
        latest = rows[-1] if rows else {}
        occupied = int(latest.get("occupied") or 0)
        waiting = int(latest.get("waiting") or 0)
        return {
            "items": rows,
            "latest": {
                "occupied": occupied,
                "running": max(0, occupied - waiting),
                "waiting": waiting,
                "label": f"占用 {occupied}（运行 {max(0, occupied - waiting)} / 等待 {waiting}）",
            },
        }

    def _utilization(self, hours: int) -> float:
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()
        rows = self.repo.query(
            "SELECT occupied, limit_effective FROM dispatch_capacity WHERE at >= ?",
            (cutoff,),
        )
        capacity = sum(int(row["limit_effective"]) for row in rows)
        return round(sum(int(row["occupied"]) for row in rows) / capacity, 3) if capacity else 0.0
