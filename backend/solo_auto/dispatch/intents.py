"""At-most-once side-effect intent log."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from solo_auto.store.repo import Repository


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class Intent:
    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    state: str
    created_at: str
    resolved_at: str
    result: dict[str, Any]


class IntentLog:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def begin(self, kind: str, key: str, payload: dict[str, Any] | None = None) -> Intent:
        existing = self.get(kind, key)
        if existing and existing.state == "pending":
            return existing
        if existing and existing.state == "done":
            return existing
        self.repo.execute(
            """
            INSERT INTO dispatch_intents(kind, key, payload, state, created_at, resolved_at, result)
            VALUES(?, ?, ?, 'pending', ?, NULL, '{}')
            ON CONFLICT(kind, key) DO UPDATE SET
              payload=excluded.payload, state='pending', created_at=excluded.created_at,
              resolved_at=NULL, result='{}'
            """,
            (kind, key, json.dumps(payload or {}, ensure_ascii=False), _now()),
        )
        intent = self.get(kind, key)
        if intent is None:
            raise RuntimeError("无法写入副作用意图")
        return intent

    def finish(
        self,
        kind: str,
        key: str,
        *,
        state: str = "done",
        result: dict[str, Any] | None = None,
    ) -> Intent | None:
        intent = self.get(kind, key)
        if intent is None:
            return None
        self.repo.execute(
            "UPDATE dispatch_intents SET state=?, resolved_at=?, result=? WHERE id=?",
            (state, _now(), json.dumps(result or {}, ensure_ascii=False), intent.id),
        )
        return self.get(kind, key)

    def get(self, kind: str, key: str) -> Intent | None:
        row = self.repo.query_one("SELECT * FROM dispatch_intents WHERE kind=? AND key=?", (kind, key))
        return _intent(row) if row else None

    def pending(self) -> list[Intent]:
        return [_intent(row) for row in self.repo.query("SELECT * FROM dispatch_intents WHERE state='pending'")]


def _intent(row: dict[str, Any]) -> Intent:
    def decode(value: Any) -> dict[str, Any]:
        try:
            data = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    return Intent(
        id=int(row["id"]),
        kind=str(row["kind"]),
        key=str(row["key"]),
        payload=decode(row.get("payload")),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        resolved_at=str(row.get("resolved_at") or ""),
        result=decode(row.get("result")),
    )
