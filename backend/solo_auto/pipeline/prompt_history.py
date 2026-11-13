"""Prompts already written on an engineering, so reusing it never repeats a question."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from solo_auto.store.repo import Repository

PROMPT_FILE = Path("delivery") / "提示词.md"
# 同工程的真实题面两两之间 ratio≈0.1、二元组 Jaccard≈0.05；换个说法重复同一需求通常都在 0.5 以上。
DUPLICATE_RATIO = 0.55
DUPLICATE_JACCARD = 0.45
NOTE_LIMIT = 8
NOTE_CHARS = 240


@dataclass(frozen=True)
class PriorPrompt:
    task_id: str
    task_name: str
    sub_direction: str
    task_type: str
    text: str


@dataclass(frozen=True)
class DuplicateHit:
    prior: PriorPrompt
    ratio: float
    jaccard: float

    @property
    def reason(self) -> str:
        return (
            f"题面与同工程已出过的「{self.prior.task_name}」雷同"
            f"（相似度 {self.ratio:.2f}，片段重合 {self.jaccard:.2f}）"
        )


class PromptHistory:
    """Reads prompts from live task roots and keeps a copy so purged tasks still count."""

    def __init__(self, repo: Repository, path: Path) -> None:
        self.repo = repo
        self.path = path
        self._lock = threading.RLock()

    def prior(self, task_id: str) -> list[PriorPrompt]:
        engineering_id = self._engineering_id(task_id)
        if not engineering_id:
            return []
        found: dict[str, PriorPrompt] = {}
        for record in self._records():
            if record.get("engineering_id") == engineering_id and record.get("task_id") != task_id:
                prior = PriorPrompt(
                    task_id=str(record.get("task_id") or ""),
                    task_name=str(record.get("task_name") or ""),
                    sub_direction=str(record.get("sub_direction") or ""),
                    task_type=str(record.get("task_type") or ""),
                    text=str(record.get("text") or ""),
                )
                if prior.text:
                    found[prior.task_id] = prior
        rows = self.repo.query(
            "SELECT id, task_name, task_root, sub_direction, task_type FROM tasks "
            "WHERE engineering_id = ? AND id != ? ORDER BY created_at",
            (engineering_id, task_id),
        )
        for row in rows:
            text = _read_prompt(Path(str(row["task_root"])))
            if text:
                found[str(row["id"])] = PriorPrompt(
                    task_id=str(row["id"]),
                    task_name=str(row["task_name"] or ""),
                    sub_direction=str(row["sub_direction"] or ""),
                    task_type=str(row["task_type"] or ""),
                    text=text,
                )
        return list(found.values())

    def notes(self, task_id: str) -> list[str]:
        prior = self.prior(task_id)
        if not prior:
            return []
        lines = [
            f"这个工程已经出过 {len(prior)} 道题。新题必须换一个业务问题、冲突点和改动模块，"
            "不得只改说法、换字段名或调换顺序；安装题面前逐条对照下列旧题自查，相近就重新设计：",
        ]
        for item in prior[-NOTE_LIMIT:]:
            label = " / ".join(part for part in (item.sub_direction, item.task_type) if part)
            text = " ".join(item.text.split())
            if len(text) > NOTE_CHARS:
                text = text[:NOTE_CHARS] + "…"
            lines.append(f"- {f'[{label}] ' if label else ''}{text}")
        return lines

    def duplicate(self, task_id: str, root: Path) -> DuplicateHit | None:
        text = _read_prompt(root)
        if not text:
            return None
        worst: DuplicateHit | None = None
        for prior in self.prior(task_id):
            ratio, jaccard = similarity(text, prior.text)
            similar = ratio >= DUPLICATE_RATIO or jaccard >= DUPLICATE_JACCARD
            if similar and (worst is None or ratio > worst.ratio):
                worst = DuplicateHit(prior, ratio, jaccard)
        return worst

    def remember(self, task_id: str, root: Path) -> None:
        text = _read_prompt(root)
        engineering_id = self._engineering_id(task_id)
        if not text or not engineering_id:
            return
        with self._lock:
            if any(record.get("task_id") == task_id for record in self._records()):
                return
            task = self.repo.get_task(task_id) or {}
            prior = PriorPrompt(
                task_id=task_id,
                task_name=str(task.get("task_name") or ""),
                sub_direction=str(task.get("sub_direction") or ""),
                task_type=str(task.get("task_type") or ""),
                text=text,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "engineering_id": engineering_id,
                "project_id": str(task.get("project_id") or ""),
                "created_at": str(task.get("created_at") or ""),
                **asdict(prior),
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def backfill(self) -> int:
        with self._lock:
            known = {str(record.get("task_id") or "") for record in self._records()}
            rows = self.repo.query(
                "SELECT id, task_root FROM tasks WHERE exported_at IS NOT NULL AND engineering_id IS NOT NULL"
            )
            added = 0
            for row in rows:
                if str(row["id"]) not in known and _read_prompt(Path(str(row["task_root"]))):
                    self.remember(str(row["id"]), Path(str(row["task_root"])))
                    added += 1
            return added

    def project_library(self, project_id: str) -> list[dict[str, Any]]:
        """Return every used prompt attributed to one project, including purged tasks."""
        if not project_id:
            return []
        with self._lock:
            items: dict[str, dict[str, Any]] = {}
            engineering_names = {
                str(row["engineering_id"]): str(row.get("engineering_name") or "")
                for row in self.repo.query(
                    """
                    SELECT ledger.engineering_id, ledger.engineering_name
                    FROM usage_ledger ledger
                    JOIN (
                        SELECT engineering_id, MAX(id) AS id
                        FROM usage_ledger
                        WHERE project_id = ?
                        GROUP BY engineering_id
                    ) latest ON latest.id = ledger.id
                    """,
                    (project_id,),
                )
            }

            def engineering_name(engineering_id: str, fallback: str) -> str:
                if not engineering_id:
                    return "未关联工程"
                return engineering_names.get(engineering_id) or fallback or engineering_id

            for record in self._records():
                if self._record_project_id(record) != project_id:
                    continue
                text = str(record.get("text") or "").strip()
                if not text:
                    continue
                engineering_id = str(record.get("engineering_id") or "")
                task_id = str(record.get("task_id") or "")
                key = f"task:{task_id}" if task_id else f"text:{_prompt_hash(project_id, text)}"
                item = {
                    "recordId": key,
                    "projectId": project_id,
                    "engineeringId": engineering_id,
                    "engineeringName": engineering_name(
                        engineering_id,
                        str(record.get("engineering_name") or record.get("task_name") or ""),
                    ),
                    "taskId": task_id,
                    "taskName": str(record.get("task_name") or ""),
                    "subDirection": str(record.get("sub_direction") or ""),
                    "taskType": str(record.get("task_type") or ""),
                    "text": text,
                    "usedAt": str(
                        record.get("created_at")
                        or record.get("imported_at")
                        or record.get("usedAt")
                        or ""
                    ),
                    "source": str(record.get("source") or "history"),
                }
                items[key] = item

            rows = self.repo.query(
                """
                SELECT id, task_name, task_root, engineering_id, project_id,
                       sub_direction, task_type, created_at
                FROM tasks
                WHERE project_id = ? AND prompt_sha256 IS NOT NULL AND prompt_sha256 != ''
                ORDER BY created_at DESC
                """,
                (project_id,),
            )
            for row in rows:
                text = _read_prompt(Path(str(row["task_root"])))
                if not text:
                    continue
                engineering_id = str(row.get("engineering_id") or "")
                task_id = str(row["id"])
                key = f"task:{task_id}"
                item = {
                    "recordId": key,
                    "projectId": project_id,
                    "engineeringId": engineering_id,
                    "engineeringName": engineering_name(
                        engineering_id,
                        str(row.get("task_name") or ""),
                    ),
                    "taskId": task_id,
                    "taskName": str(row.get("task_name") or ""),
                    "subDirection": str(row.get("sub_direction") or ""),
                    "taskType": str(row.get("task_type") or ""),
                    "text": text,
                    "usedAt": str(row.get("created_at") or ""),
                    "source": "live",
                }
                if key in items:
                    items[key].update({key_name: value for key_name, value in item.items() if value})
                else:
                    items[key] = item
        return sorted(
            items.values(),
            key=lambda item: (str(item.get("usedAt") or ""), str(item.get("taskName") or "")),
            reverse=True,
        )

    def import_library(
        self,
        project_id: str,
        items: list[dict[str, Any]],
        *,
        source_name: str = "",
    ) -> dict[str, Any]:
        existing = {
            _prompt_hash(project_id, str(item.get("text") or ""))
            for item in self.project_library(project_id)
        }
        imported: list[dict[str, Any]] = []
        skipped = 0
        errors: list[str] = []
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._lock:
            for index, raw in enumerate(items, 1):
                text = str(
                    raw.get("text")
                    or raw.get("prompt")
                    or raw.get("userPrompt")
                    or ""
                ).strip()
                if not text:
                    errors.append(f"第 {index} 条缺少提示词正文")
                    skipped += 1
                    continue
                if len(text) > 5000:
                    errors.append(f"第 {index} 条提示词超过 5000 字")
                    skipped += 1
                    continue
                digest = _prompt_hash(project_id, text)
                if digest in existing:
                    skipped += 1
                    continue
                existing.add(digest)
                record = {
                    "project_id": project_id,
                    "origin_project_id": str(raw.get("projectId") or ""),
                    "engineering_id": str(raw.get("engineeringId") or ""),
                    "engineering_name": str(raw.get("engineeringName") or ""),
                    "task_id": str(raw.get("taskId") or ""),
                    "task_name": str(raw.get("taskName") or ""),
                    "sub_direction": str(raw.get("subDirection") or ""),
                    "task_type": str(raw.get("taskType") or ""),
                    "text": text,
                    "created_at": str(raw.get("usedAt") or now),
                    "imported_at": now,
                    "source": "import",
                    "source_file": source_name,
                }
                imported.append(record)
            if imported:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    for record in imported:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {
            "imported": len(imported),
            "skipped": skipped,
            "errors": errors,
            "total": len(existing),
        }

    def _engineering_id(self, task_id: str) -> str:
        task = self.repo.get_task(task_id)
        if task and task.get("engineering_id"):
            return str(task["engineering_id"])
        row = self.repo.query_one(
            "SELECT engineering_id FROM usage_ledger WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(row["engineering_id"]) if row and row.get("engineering_id") else ""

    def _record_project_id(self, record: dict[str, Any]) -> str:
        project_id = str(record.get("project_id") or "")
        if project_id:
            return project_id
        task_id = str(record.get("task_id") or "")
        if task_id:
            task = self.repo.get_task(task_id)
            if task and task.get("project_id"):
                return str(task["project_id"])
            row = self.repo.query_one(
                "SELECT project_id FROM usage_ledger WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            if row and row.get("project_id"):
                return str(row["project_id"])
        engineering_id = str(record.get("engineering_id") or "")
        if engineering_id:
            row = self.repo.query_one(
                "SELECT project_id FROM usage_ledger WHERE engineering_id = ? ORDER BY id DESC LIMIT 1",
                (engineering_id,),
            )
            if row and row.get("project_id"):
                return str(row["project_id"])
        return ""

    def _records(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records


def similarity(left: str, right: str) -> tuple[float, float]:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0, 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    grams_a = {a[index : index + 2] for index in range(len(a) - 1)}
    grams_b = {b[index : index + 2] for index in range(len(b) - 1)}
    union = grams_a | grams_b
    jaccard = len(grams_a & grams_b) / len(union) if union else 0.0
    return round(ratio, 4), round(jaccard, 4)


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.lower())


def _read_prompt(root: Path) -> str:
    try:
        return (root / PROMPT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _prompt_hash(project_id: str, text: str) -> str:
    payload = f"{project_id}\0{_normalize(text)}".encode()
    return hashlib.sha256(payload).hexdigest()
