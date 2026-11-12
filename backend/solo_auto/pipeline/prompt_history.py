"""Prompts already written on an engineering, so reusing it never repeats a question."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
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
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"engineering_id": engineering_id, **asdict(prior)}, ensure_ascii=False) + "\n")

    def backfill(self) -> int:
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

    def _engineering_id(self, task_id: str) -> str:
        task = self.repo.get_task(task_id)
        if task and task.get("engineering_id"):
            return str(task["engineering_id"])
        row = self.repo.query_one(
            "SELECT engineering_id FROM usage_ledger WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return str(row["engineering_id"]) if row and row.get("engineering_id") else ""

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
