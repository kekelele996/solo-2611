"""Small hand-written SQLite index. Disk state always wins."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  task_root TEXT NOT NULL UNIQUE,
  task_name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  engineering_id TEXT,
  project_id TEXT,
  claim_id TEXT,
  domain TEXT NOT NULL,
  sub_direction TEXT NOT NULL,
  task_type TEXT,
  difficulty TEXT,
  languages TEXT,
  repro_level TEXT,
  job_status TEXT NOT NULL,
  skill_status TEXT,
  skill_version TEXT,
  initial_sha TEXT,
  artifact_sha TEXT,
  session_id TEXT,
  prompt_id TEXT,
  prompt_sha256 TEXT,
  feedback_score INTEGER,
  ack_by TEXT,
  review_acked_at TEXT,
  exported_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  notes TEXT,
  presence TEXT NOT NULL DEFAULT 'present'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(job_status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id) WHERE session_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS steps (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  step_no INTEGER NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, step_no)
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  mode TEXT NOT NULL,
  container TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  outcome TEXT NOT NULL,
  error_kind TEXT,
  detail TEXT,
  UNIQUE (task_id, seq)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER,
  sha256 TEXT,
  meta TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, kind);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  level TEXT NOT NULL,
  actor TEXT NOT NULL,
  task_id TEXT,
  action TEXT NOT NULL,
  message TEXT NOT NULL,
  details TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, at DESC);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  engineering_id TEXT NOT NULL,
  engineering_name TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  released_at TEXT,
  task_id TEXT REFERENCES tasks(id),
  zip_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS autopilot_stages (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  outcome TEXT,
  failure_kind TEXT,
  failure_detail TEXT,
  usage_json TEXT,
  log_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_ap_stages_task ON autopilot_stages(task_id, started_at);
CREATE TABLE IF NOT EXISTS audits (
  task_id TEXT PRIMARY KEY,
  sampled INTEGER NOT NULL,
  verdict TEXT,
  reason TEXT,
  disposition TEXT,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  xlsx_path TEXT,
  check_json TEXT NOT NULL,
  unaudited_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
  batch_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  PRIMARY KEY (batch_id, task_id)
);
CREATE TABLE IF NOT EXISTS usage_ledger (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  engineering_id TEXT NOT NULL,
  engineering_name TEXT NOT NULL,
  task_id TEXT,
  task_name TEXT,
  claimed_at TEXT NOT NULL,
  task_created_at TEXT,
  delivered_at TEXT,
  submitted_at TEXT,
  outcome TEXT NOT NULL,
  failure_kind TEXT,
  purged_at TEXT,
  backfilled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_ledger(source, project_id);
CREATE INDEX IF NOT EXISTS idx_usage_eng ON usage_ledger(source, engineering_id);
CREATE TABLE IF NOT EXISTS dispatch_items (
  task_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  position REAL NOT NULL,
  enqueued_at TEXT NOT NULL,
  not_before TEXT,
  wait_code TEXT,
  wait_text TEXT,
  wait_until TEXT,
  attempts TEXT NOT NULL DEFAULT '{}',
  total_attempts INTEGER NOT NULL DEFAULT 0,
  last_failure TEXT,
  repeat_signature TEXT,
  guard_flags TEXT NOT NULL DEFAULT '[]',
  keep INTEGER NOT NULL DEFAULT 0,
  plan_id TEXT,
  project_id TEXT,
  engineering_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_items_order
  ON dispatch_items(status, priority DESC, position ASC, enqueued_at ASC);
CREATE TABLE IF NOT EXISTS dispatch_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  result TEXT,
  UNIQUE(kind, key)
);
CREATE TABLE IF NOT EXISTS dispatch_outcomes (
  task_id TEXT,
  finished_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  queued_s REAL,
  authoring_s REAL,
  wait_container_s REAL,
  running_s REAL,
  delivering_s REAL,
  recording_s REAL,
  exporting_s REAL,
  total_s REAL,
  attempts INTEGER,
  failure_kind TEXT,
  project_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_dispatch_outcomes_at ON dispatch_outcomes(finished_at DESC);
CREATE TABLE IF NOT EXISTS dispatch_capacity (
  at TEXT PRIMARY KEY,
  occupied INTEGER NOT NULL,
  limit_effective INTEGER NOT NULL,
  limit_floor INTEGER NOT NULL,
  in_hand INTEGER NOT NULL,
  sessions INTEGER NOT NULL,
  held TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engineering_cooldown (
  engineering_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  until TEXT NOT NULL,
  reason TEXT NOT NULL,
  failures INTEGER NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Repository:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(DDL)
            self._migrate()
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, ?)",
                (now(),),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(2, ?)",
                (now(),),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(3, ?)",
                (now(),),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()}
        if "ack_by" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN ack_by TEXT")
        if "presence" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN presence TEXT NOT NULL DEFAULT 'present'")
        self._connection.execute("PRAGMA user_version=3")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connection:
            yield self._connection

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def add_event(
        self,
        action: str,
        message: str,
        *,
        level: str = "info",
        actor: str = "system",
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        return self.execute(
            """
            INSERT INTO events(at, level, actor, task_id, action, message, details)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (now(), level, actor, task_id, action, message, json.dumps(details or {}, ensure_ascii=False)),
        )

    def events(
        self,
        *,
        task_id: str | None = None,
        level: str | None = None,
        since_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["id > ?"]
        params: list[Any] = [since_id]
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if level:
            clauses.append("level = ?")
            params.append(level)
        params.append(max(1, min(limit, 1000)))
        rows = self.query(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        for row in rows:
            try:
                row["details"] = json.loads(row.get("details") or "{}")
            except json.JSONDecodeError:
                row["details"] = {}
        return list(reversed(rows))

    def task_id_for_root(self, task_root: Path) -> str:
        existing = self.query_one("SELECT id FROM tasks WHERE task_root = ?", (str(task_root),))
        if existing:
            return str(existing["id"])
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(task_root.resolve())))

    def upsert_task(self, record: dict[str, Any], *, notes: str | None = None) -> str:
        task_id = str(record["id"])
        existing = self.query_one("SELECT * FROM tasks WHERE id = ? OR task_root = ?", (task_id, record["task_root"]))
        timestamp = now()
        if existing:
            task_id = str(existing["id"])
            merged = dict(existing)
            merged.update(record)
            merged["id"] = task_id
            merged["updated_at"] = timestamp
            if notes is not None:
                merged["notes"] = notes
            with self.transaction() as conn:
                conn.execute(
                    """
                    UPDATE tasks SET
                      task_root=:task_root, task_name=:task_name, source_kind=:source_kind,
                      engineering_id=:engineering_id, project_id=:project_id, claim_id=:claim_id,
                      domain=:domain, sub_direction=:sub_direction, task_type=:task_type,
                      difficulty=:difficulty, languages=:languages, repro_level=:repro_level,
                      job_status=:job_status, skill_status=:skill_status, skill_version=:skill_version,
                      initial_sha=:initial_sha, artifact_sha=:artifact_sha, session_id=:session_id,
                      prompt_id=:prompt_id, prompt_sha256=:prompt_sha256, feedback_score=:feedback_score,
                      review_acked_at=:review_acked_at, exported_at=:exported_at, updated_at=:updated_at,
                      notes=:notes, ack_by=:ack_by, presence=:presence
                    WHERE id=:id
                    """,
                    _task_params(merged),
                )
            return task_id
        record = dict(record)
        record["id"] = task_id
        record.setdefault("created_at", timestamp)
        record["updated_at"] = timestamp
        record.setdefault("notes", notes)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                  id, task_root, task_name, source_kind, engineering_id, project_id, claim_id,
                  domain, sub_direction, task_type, difficulty, languages, repro_level,
                  job_status, skill_status, skill_version, initial_sha, artifact_sha,
                  session_id, prompt_id, prompt_sha256, feedback_score, review_acked_at,
                  exported_at, created_at, updated_at, notes, ack_by, presence
                ) VALUES(
                  :id, :task_root, :task_name, :source_kind, :engineering_id, :project_id, :claim_id,
                  :domain, :sub_direction, :task_type, :difficulty, :languages, :repro_level,
                  :job_status, :skill_status, :skill_version, :initial_sha, :artifact_sha,
                  :session_id, :prompt_id, :prompt_sha256, :feedback_score, :review_acked_at,
                  :exported_at, :created_at, :updated_at, :notes, :ack_by, :presence
                )
                """,
                _task_params(record),
            )
        return task_id

    def replace_steps(self, task_id: str, steps: list[dict[str, Any]]) -> None:
        stamp = now()
        with self.transaction() as conn:
            conn.execute("DELETE FROM steps WHERE task_id = ?", (task_id,))
            conn.executemany(
                "INSERT INTO steps(task_id, step_no, state, detail, updated_at) VALUES(?, ?, ?, ?, ?)",
                [
                    (task_id, int(step["number"]), str(step["state"]), str(step.get("detail") or ""), stamp)
                    for step in steps
                ],
            )

    def set_job_status(self, task_id: str, status: str, *, notes: str | None = None) -> None:
        fields = "job_status = ?, updated_at = ?"
        params: list[Any] = [status, now()]
        if notes is not None:
            fields += ", notes = ?"
            params.append(notes)
        params.append(task_id)
        self.execute(f"UPDATE tasks SET {fields} WHERE id = ?", tuple(params))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def get_task_by_root(self, root: Path) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM tasks WHERE task_root = ?", (str(root),))

    def steps(self, task_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM steps WHERE task_id = ? ORDER BY step_no", (task_id,))

    def list_tasks(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        project: str | None = None,
        date: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("job_status = ?")
            params.append(status)
        if q:
            clauses.append("(task_name LIKE ? OR task_root LIKE ? OR session_id LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if project:
            clauses.append("project_id = ?")
            params.append(project)
        if date:
            clauses.append("substr(created_at, 1, 10) = ?")
            params.append(date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = self.query_one(f"SELECT COUNT(*) AS count FROM tasks {where}", tuple(params))
        total = int(total_row["count"]) if total_row else 0
        offset = (max(1, page) - 1) * page_size
        rows = self.query(
            f"SELECT * FROM tasks {where} ORDER BY CASE job_status "
            "WHEN 'blocked' THEN 0 WHEN 'running' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END, updated_at DESC "
            "LIMIT ? OFFSET ?",
            tuple([*params, page_size, offset]),
        )
        for row in rows:
            row["steps"] = self.steps(str(row["id"]))
        return rows, total

    def session_counts(self) -> dict[str, int]:
        rows = self.query(
            "SELECT session_id, COUNT(*) AS n FROM tasks WHERE session_id IS NOT NULL GROUP BY session_id"
        )
        return {str(row["session_id"]): int(row["n"]) for row in rows}

    def upsert_claim(self, claim: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO claims(id, project_id, engineering_id, engineering_name, claimed_at, released_at, task_id, zip_sha256)
                VALUES(:id, :project_id, :engineering_id, :engineering_name, :claimed_at, :released_at, :task_id, :zip_sha256)
                ON CONFLICT(id) DO UPDATE SET
                  released_at=excluded.released_at, task_id=excluded.task_id, zip_sha256=excluded.zip_sha256
                """,
                claim,
            )

    def start_autopilot_stage(
        self,
        *,
        task_id: str,
        stage: str,
        attempt: int,
        log_path: str,
    ) -> int:
        return self.execute(
            """
            INSERT INTO autopilot_stages(task_id, stage, attempt, started_at, log_path)
            VALUES(?, ?, ?, ?, ?)
            """,
            (task_id, stage, attempt, now(), log_path),
        )

    def finish_autopilot_stage(
        self,
        stage_id: int,
        *,
        outcome: str,
        failure_kind: str = "",
        failure_detail: str = "",
        usage_data: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE autopilot_stages SET ended_at=?, outcome=?, failure_kind=?, failure_detail=?, usage_json=?
            WHERE id=?
            """,
            (
                now(),
                outcome,
                failure_kind,
                failure_detail[:4096],
                json.dumps(usage_data, ensure_ascii=False) if usage_data else None,
                stage_id,
            ),
        )

    def autopilot_stages(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM autopilot_stages WHERE task_id=? ORDER BY started_at DESC, id DESC",
            (task_id,),
        )
        for row in rows:
            with suppress(json.JSONDecodeError):
                row["usage_json"] = json.loads(row["usage_json"]) if row.get("usage_json") else None
        return rows

    def upsert_audit(self, task_id: str, audit: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audits(task_id, sampled, verdict, reason, disposition, decided_at)
                VALUES(:task_id, :sampled, :verdict, :reason, :disposition, :decided_at)
                ON CONFLICT(task_id) DO UPDATE SET
                  sampled=excluded.sampled, verdict=excluded.verdict, reason=excluded.reason,
                  disposition=excluded.disposition, decided_at=excluded.decided_at
                """,
                {"task_id": task_id, **audit},
            )

    def audit(self, task_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM audits WHERE task_id=?", (task_id,))

    def audits(self, verdict: str | None = None) -> list[dict[str, Any]]:
        if verdict:
            return self.query("SELECT * FROM audits WHERE verdict=? ORDER BY decided_at DESC", (verdict,))
        return self.query("SELECT * FROM audits ORDER BY decided_at DESC")

    def insert_batch(
        self,
        *,
        batch_id: str,
        created_at: str,
        xlsx_path: str,
        check: dict[str, Any],
        task_ids: list[str],
        unaudited_count: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO batches(id, created_at, xlsx_path, check_json, unaudited_count)
                VALUES(?, ?, ?, ?, ?)
                """,
                (batch_id, created_at, xlsx_path, json.dumps(check, ensure_ascii=False), unaudited_count),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO batch_items(batch_id, task_id) VALUES(?, ?)",
                [(batch_id, task_id) for task_id in task_ids],
            )

    def batches(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM batches ORDER BY created_at DESC")
        for row in rows:
            try:
                row["check"] = json.loads(row.pop("check_json") or "{}")
            except json.JSONDecodeError:
                row["check"] = {}
            row["task_ids"] = [
                item["task_id"]
                for item in self.query("SELECT task_id FROM batch_items WHERE batch_id=?", (row["id"],))
            ]
        return rows

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        return next((row for row in self.batches() if row["id"] == batch_id), None)

    def task_in_any_batch(self, task_id: str) -> bool:
        return self.query_one("SELECT 1 AS found FROM batch_items WHERE task_id=? LIMIT 1", (task_id,)) is not None

    def mark_presence(self, task_id: str, presence: str) -> None:
        self.execute("UPDATE tasks SET presence=?, updated_at=? WHERE id=?", (presence, now(), task_id))

    def prune_events(self, days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM events WHERE at < ? AND level != 'error'", (cutoff,))
            return int(cursor.rowcount)

    def stats(self, start: str, end: str) -> dict[str, Any]:
        tasks = self.query(
            """
            SELECT substr(created_at, 1, 10) AS date, COUNT(*) AS created,
                   SUM(CASE WHEN job_status = 'exported' THEN 1 ELSE 0 END) AS exported,
                   SUM(CASE WHEN skill_status = 'run_failed' THEN 1 ELSE 0 END) AS failed
            FROM tasks WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
            GROUP BY substr(created_at, 1, 10) ORDER BY date
            """,
            (start, end),
        )
        total = (
            self.query_one(
                """
            SELECT COUNT(*) AS created,
                   SUM(CASE WHEN exported_at IS NOT NULL THEN 1 ELSE 0 END) AS exported,
                   AVG(CASE WHEN exported_at IS NOT NULL
                       THEN (julianday(exported_at) - julianday(created_at)) * 24 * 60 END) AS average_minutes
            FROM tasks WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
            """,
                (start, end),
            )
            or {}
        )
        failures = self.query(
            """
            SELECT action, message, COUNT(*) AS count
            FROM events
            WHERE level IN ('warn', 'error') AND substr(at, 1, 10) BETWEEN ? AND ?
            GROUP BY action, message ORDER BY count DESC LIMIT 20
            """,
            (start, end),
        )
        return {
            "days": tasks,
            "summary": {
                "created": int(total.get("created") or 0),
                "exported": int(total.get("exported") or 0),
                "average_minutes": round(float(total.get("average_minutes") or 0), 1),
            },
            "failures": failures,
        }


def _task_params(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "task_root",
        "task_name",
        "source_kind",
        "engineering_id",
        "project_id",
        "claim_id",
        "domain",
        "sub_direction",
        "task_type",
        "difficulty",
        "languages",
        "repro_level",
        "job_status",
        "skill_status",
        "skill_version",
        "initial_sha",
        "artifact_sha",
        "session_id",
        "prompt_id",
        "prompt_sha256",
        "feedback_score",
        "ack_by",
        "review_acked_at",
        "exported_at",
        "created_at",
        "updated_at",
        "notes",
        "presence",
    )
    values = {key: record.get(key) for key in keys}
    values["ack_by"] = record.get("ack_by") or ""
    values["presence"] = record.get("presence") or "present"
    return values
