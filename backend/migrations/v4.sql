-- solo-auto v4 managed-session migration.
-- Repository._migrate applies the same statements idempotently so a process
-- killed between ALTER statements can safely resume on the next start.

ALTER TABLE attempts ADD COLUMN session_run_status TEXT;
ALTER TABLE attempts ADD COLUMN rounds INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN continues INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN start_failed INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN interrupt_kind TEXT;
ALTER TABLE attempts ADD COLUMN last_activity_at TEXT;

CREATE TABLE IF NOT EXISTS session_rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_seq INTEGER NOT NULL,
  round_no INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  stop_reason TEXT,
  kind TEXT NOT NULL,
  verdict TEXT NOT NULL,
  detail TEXT,
  tool_uses INTEGER DEFAULT 0,
  final_chars INTEGER DEFAULT 0,
  continued INTEGER DEFAULT 0,
  UNIQUE (task_id, attempt_seq, round_no)
);

CREATE INDEX IF NOT EXISTS idx_session_rounds_task
  ON session_rounds(task_id, attempt_seq, round_no);

ALTER TABLE dispatch_items ADD COLUMN session_state TEXT;
ALTER TABLE dispatch_items ADD COLUMN continue_used INTEGER DEFAULT 0;
ALTER TABLE dispatch_items ADD COLUMN next_action_at TEXT;
ALTER TABLE dispatch_items ADD COLUMN container_seconds INTEGER DEFAULT 0;

ALTER TABLE dispatch_capacity ADD COLUMN waiting INTEGER DEFAULT 0;

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES(4, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

PRAGMA user_version=4;
