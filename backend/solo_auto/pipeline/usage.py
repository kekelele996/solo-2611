"""Durable per-engineering usage ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from solo_auto.store.repo import Repository


class UsageLedger:
    def __init__(self, repo: Repository, source: str) -> None:
        self.repo = repo
        self.source = source.rstrip("/")

    def record_claim(self, meta: dict[str, Any]) -> None:
        self.repo.execute(
            """
            INSERT INTO usage_ledger(
              source, project_id, project_name, engineering_id, engineering_name,
              claimed_at, outcome, backfilled
            ) VALUES(?, ?, ?, ?, ?, ?, 'claimed', 0)
            """,
            (
                self.source,
                str(meta.get("projectId") or ""),
                str(meta.get("projectName") or meta.get("projectId") or ""),
                str(meta.get("engineeringId") or ""),
                str(meta.get("name") or meta.get("engineeringId") or ""),
                str(meta.get("downloadedAt") or _now()),
            ),
        )

    def attach_task(self, meta: dict[str, Any], task_id: str, task_name: str) -> None:
        row = self.repo.query_one(
            """
            SELECT id FROM usage_ledger
            WHERE source=? AND project_id=? AND engineering_id=? AND task_id IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (self.source, str(meta.get("projectId") or ""), str(meta.get("engineeringId") or "")),
        )
        if row:
            self.repo.execute(
                """
                UPDATE usage_ledger SET task_id=?, task_name=?, task_created_at=?, outcome='in_progress'
                WHERE id=?
                """,
                (task_id, task_name, _now(), row["id"]),
            )
        else:
            self.repo.execute(
                """
                INSERT INTO usage_ledger(
                  source, project_id, project_name, engineering_id, engineering_name,
                  task_id, task_name, claimed_at, task_created_at, outcome, backfilled
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', 0)
                """,
                (
                    self.source,
                    str(meta.get("projectId") or ""),
                    str(meta.get("projectName") or meta.get("projectId") or ""),
                    str(meta.get("engineeringId") or ""),
                    str(meta.get("name") or meta.get("engineeringId") or ""),
                    task_id,
                    task_name,
                    _now(),
                    _now(),
                ),
            )

    def mark_delivered(self, task_id: str) -> None:
        self.repo.execute(
            """
            UPDATE usage_ledger SET delivered_at=COALESCE(delivered_at, ?), outcome='delivered'
            WHERE task_id=?
            """,
            (_now(), task_id),
        )

    def mark_submitted(self, task_id: str) -> None:
        self.repo.execute(
            """
            UPDATE usage_ledger
            SET delivered_at=COALESCE(delivered_at, ?), submitted_at=COALESCE(submitted_at, ?), outcome='submitted'
            WHERE task_id=?
            """,
            (_now(), _now(), task_id),
        )

    def mark_failure(self, task_id: str, failure_kind: str) -> None:
        self.repo.execute(
            "UPDATE usage_ledger SET outcome='failed', failure_kind=? WHERE task_id=?",
            (failure_kind, task_id),
        )

    def mark_outcome(self, task_id: str, outcome: str) -> None:
        self.repo.execute("UPDATE usage_ledger SET outcome=? WHERE task_id=?", (outcome, task_id))

    def mark_purged(self, task_ids: list[str]) -> None:
        if not task_ids:
            return
        marks = ",".join("?" for _ in task_ids)
        self.repo.execute(
            f"UPDATE usage_ledger SET purged_at=?, outcome=CASE WHEN submitted_at IS NOT NULL THEN 'submitted' "
            f"WHEN delivered_at IS NOT NULL THEN 'delivered' ELSE outcome END WHERE task_id IN ({marks})",
            (_now(), *task_ids),
        )

    def mark_released(self, project_id: str, engineering_id: str) -> None:
        self.repo.execute(
            """
            UPDATE usage_ledger SET outcome='released'
            WHERE task_id IS NULL AND id=(
              SELECT MAX(id) FROM usage_ledger WHERE source=? AND project_id=? AND engineering_id=? AND task_id IS NULL
            )
            """,
            (self.source, project_id, engineering_id),
        )

    def backfill(self) -> int:
        inserted = 0
        for task in self.repo.query("SELECT * FROM tasks"):
            existing = self.repo.query_one("SELECT * FROM usage_ledger WHERE task_id=? LIMIT 1", (task["id"],))
            if existing:
                if not existing.get("project_id") or not existing.get("project_name"):
                    self.repo.execute(
                        """
                        UPDATE usage_ledger
                        SET project_id='local', project_name='本地任务',
                            engineering_id=CASE WHEN engineering_id='' THEN ? ELSE engineering_id END,
                            engineering_name=CASE WHEN engineering_name='' THEN ? ELSE engineering_name END
                        WHERE id=?
                        """,
                        (
                            str(task.get("task_root") or task["id"]),
                            str(task.get("task_name") or "本地任务"),
                            existing["id"],
                        ),
                    )
                continue
            project_id = str(task.get("project_id") or "local")
            engineering_id = str(task.get("engineering_id") or task.get("task_root") or task.get("source_kind") or "")
            if not engineering_id:
                continue
            delivered = task.get("exported_at")
            submitted = (
                self.repo.query_one("SELECT 1 AS found FROM batch_items WHERE task_id=? LIMIT 1", (task["id"],))
                is not None
            )
            outcome = "submitted" if submitted else ("delivered" if delivered else "in_progress")
            self.repo.execute(
                """
                INSERT INTO usage_ledger(
                  source, project_id, project_name, engineering_id, engineering_name,
                  task_id, task_name, claimed_at, task_created_at, delivered_at, submitted_at,
                  outcome, backfilled
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    self.source,
                    project_id,
                    "本地任务" if project_id == "local" else project_id,
                    engineering_id,
                    str(task.get("task_name") or engineering_id),
                    task["id"],
                    task.get("task_name"),
                    str(task.get("created_at") or _now()),
                    task.get("created_at"),
                    delivered,
                    _now() if submitted else None,
                    outcome,
                ),
            )
            inserted += 1
        return inserted

    def engineering_stats(self, *, project_id: str | None = None, start: str = "", end: str = "") -> list[dict[str, Any]]:
        clauses = ["source=?"]
        params: list[Any] = [self.source]
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if start:
            clauses.append("substr(claimed_at,1,10)>=?")
            params.append(start)
        if end:
            clauses.append("substr(claimed_at,1,10)<=?")
            params.append(end)
        rows = self.repo.query(
            f"""
            SELECT project_id,
                   (SELECT project_name FROM usage_ledger u4 WHERE u4.source=u.source
                    AND u4.project_id=u.project_id ORDER BY u4.id DESC LIMIT 1) AS project_name,
                   engineering_id,
                   (SELECT engineering_name FROM usage_ledger u5 WHERE u5.source=u.source
                    AND u5.project_id=u.project_id AND u5.engineering_id=u.engineering_id
                    ORDER BY u5.id DESC LIMIT 1) AS engineering_name,
                   SUM(CASE WHEN task_created_at IS NOT NULL THEN 1 ELSE 0 END) AS used,
                   SUM(CASE WHEN task_created_at IS NULL THEN 1 ELSE 0 END) AS claimed_unused,
                   SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered,
                   SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted,
                   MAX(claimed_at) AS last_used_at,
                   (SELECT outcome FROM usage_ledger u2 WHERE u2.source=u.source AND u2.project_id=u.project_id
                    AND u2.engineering_id=u.engineering_id
                    ORDER BY u2.id DESC LIMIT 1) AS last_outcome,
                   (SELECT failure_kind FROM usage_ledger u3 WHERE u3.source=u.source AND u3.project_id=u.project_id
                    AND u3.engineering_id=u.engineering_id
                    AND failure_kind != '' ORDER BY u3.id DESC LIMIT 1) AS last_failure_kind
            FROM usage_ledger u WHERE {' AND '.join(clauses)}
            GROUP BY project_id, engineering_id
            ORDER BY last_used_at DESC
            """,
            tuple(params),
        )
        for row in rows:
            row["delivery_rate"] = round(int(row["delivered"]) / max(1, int(row["used"])), 4)
        return rows

    def project_stats(self, *, start: str = "", end: str = "") -> list[dict[str, Any]]:
        clauses = ["source=?"]
        params: list[Any] = [self.source]
        if start:
            clauses.append("substr(claimed_at,1,10)>=?")
            params.append(start)
        if end:
            clauses.append("substr(claimed_at,1,10)<=?")
            params.append(end)
        rows = self.repo.query(
            f"""
            SELECT project_id,
                   (SELECT project_name FROM usage_ledger u2 WHERE u2.source=u.source
                    AND u2.project_id=u.project_id ORDER BY u2.id DESC LIMIT 1) AS project_name,
                   SUM(CASE WHEN task_created_at IS NOT NULL THEN 1 ELSE 0 END) AS used,
                   SUM(CASE WHEN task_created_at IS NULL THEN 1 ELSE 0 END) AS claimed_unused,
                   SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered,
                   SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted,
                   MAX(claimed_at) AS last_used_at
            FROM usage_ledger u WHERE {' AND '.join(clauses)}
            GROUP BY project_id
            ORDER BY last_used_at DESC
            """,
            tuple(params),
        )
        for row in rows:
            row["delivery_rate"] = round(int(row["delivered"]) / max(1, int(row["used"])), 4)
        return rows

    def first_recorded_at(self) -> str:
        row = self.repo.query_one(
            "SELECT MIN(claimed_at) AS first_at FROM usage_ledger WHERE source=?",
            (self.source,),
        )
        return str((row or {}).get("first_at") or "")

    def records(self, engineering_id: str) -> list[dict[str, Any]]:
        return self.repo.query(
            "SELECT * FROM usage_ledger WHERE source=? AND engineering_id=? ORDER BY claimed_at DESC",
            (self.source, engineering_id),
        )


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()

