"""File-backed project memory shared by future Solo 2611 task sessions.

The memory root and per-task ``monitor/context-pack.md`` are host-only orchestration
inputs.  Never copy them into a task workspace or any file mounted into a Claude
Code execution container.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.pipeline.prompt_history import PROMPT_FILE
from solo_auto.store.repo import Repository

SCHEMA_VERSION = 1
SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
}
CODE_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".go",
    ".java",
    ".rs",
    ".sql",
    ".sh",
    ".vue",
    ".css",
    ".html",
}
MAX_PROMPT_TEXT = 1200


def _safe(value: str, fallback: str) -> str:
    cleaned = SAFE_RE.sub("-", value.strip()).strip("-._")
    return (cleaned or fallback)[:96]


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _prompt_text(root: Path) -> str:
    for relative in (PROMPT_FILE, PROMPT_FILE.with_name("题面草稿.md")):
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""


def _compact(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


class ProjectMemory:
    """Persist factual engineering context and prompt history on disk."""

    def __init__(self, repo: Repository, root: Path, *, history_path: Path | None = None) -> None:
        self.repo = repo
        self.root = root
        self.history_path = history_path or root.parent / "prompt-history.jsonl"
        self._lock = threading.RLock()

    def backfill(self, *, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, hours))).replace(microsecond=0).isoformat()
        rows = self.repo.query(
            "SELECT id FROM tasks WHERE created_at >= ? OR exported_at >= ? ORDER BY created_at",
            (cutoff, cutoff),
        )
        synced: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            try:
                self.sync_task(task_id)
                synced.append(task_id)
            except Exception:
                continue
        marker = {
            "schemaVersion": SCHEMA_VERSION,
            "hours": hours,
            "cutoff": cutoff,
            "completedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "taskCount": len(synced),
            "taskIds": synced,
        }
        _write_json(self.root / ".backfill.json", marker)
        return marker

    def sync_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.repo.get_task(task_id)
        if not task:
            return None
        root = Path(str(task.get("task_root") or ""))
        if not root.is_dir():
            return None
        ledger = self.repo.query(
            "SELECT project_id, project_name, engineering_id, engineering_name, outcome, failure_kind "
            "FROM usage_ledger WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        latest_ledger = ledger[0] if ledger else {}
        project_id = str(task.get("project_id") or latest_ledger.get("project_id") or "")
        engineering_id = str(
            task.get("engineering_id") or latest_ledger.get("engineering_id") or ""
        )
        if not engineering_id:
            engineering_id = _safe(str(task.get("task_name") or task_id), task_id)
        project_dir, engineering_dir = self._directories(task_id, project_id, engineering_id)
        engineering_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(project_dir, 0o700)
        os.chmod(engineering_dir, 0o700)
        record = self._task_record(
            task,
            project_id=project_id,
            engineering_id=engineering_id,
            ledger=latest_ledger,
        )
        with self._lock:
            self._append_prompt(engineering_dir / "prompts.jsonl", record)
            records = self._engineering_records(project_id, engineering_id, fallback=record)
            self._write_project(project_dir, project_id, records)
            self._write_engineering(project_dir, engineering_dir, records)
        return record

    def notes(self, task_id: str) -> list[str]:
        record = self.sync_task(task_id)
        if not record:
            return []
        project_dir, engineering_dir = self._directories(task_id, record["projectId"], record["engineeringId"])
        history = self._prompt_records(engineering_dir / "prompts.jsonl")
        task_context = Path(str(record.get("taskRoot") or "")) / "monitor" / "context-pack.md"
        lines = [
            "项目记忆：这些分析只用于复用工程理解，不能让新题复制旧题结论。",
            f"- 本任务冻结上下文：{task_context}",
            f"- 工程分析：{engineering_dir / 'analysis.md'}",
            f"- 机器上下文：{engineering_dir / 'context.json'}",
            f"- 仓库地图：{engineering_dir / 'repo-map.md'}",
            f"- 测试手册：{engineering_dir / 'test-playbook.md'}",
            f"- 风险清单：{engineering_dir / 'risks.json'}",
            f"- 历史题面账本：{engineering_dir / 'prompts.jsonl'}",
            "执行要求：开始分析前先读取以上文件；历史提示词只用于查重和风格参考，新题必须在业务问题、冲突点、关键取舍或状态链路上有实质差异。",
            f"安全边界：不要把以上文件或摘要复制到 workspace、{PROMPT_FILE.as_posix()}、容器内文件或 Claude Code 的输入中；这些内容只供编排模型使用。",
        ]
        if history:
            lines.append(f"该工程累计记录 {len(history)} 道题面。最近题面摘要：")
            for item in history[-5:]:
                lines.append(
                    f"- [{item.get('jobStatus', '')}/{item.get('qcStatus', '')}] "
                    f"{_compact(str(item.get('taskName') or ''), 50)}："
                    f"{_compact(str(item.get('prompt') or ''), 180)}"
                )
        return lines

    def snapshot_for_task(self, task_id: str, task_root: Path) -> dict[str, str]:
        record = self.sync_task(task_id)
        if not record:
            return {}
        project_dir, engineering_dir = self._directories(task_id, record["projectId"], record["engineeringId"])
        sources = {
            "project": project_dir / "project.md",
            "analysis": engineering_dir / "analysis.md",
            "context": engineering_dir / "context.json",
            "repoMap": engineering_dir / "repo-map.md",
            "testPlaybook": engineering_dir / "test-playbook.md",
            "risks": engineering_dir / "risks.json",
            "prompts": engineering_dir / "prompts.jsonl",
        }
        parts = [
            "# 项目记忆上下文包",
            "",
            f"- 项目：{record.get('projectName') or record['projectId']}",
            f"- 工程：{record.get('engineeringName') or record['engineeringId']}",
            f"- 生成时间：{datetime.now(UTC).replace(microsecond=0).isoformat()}",
            "",
            "## 使用边界",
            "复用工程结构、测试方式和已确认风险；不得复用历史题面形成近似变体。",
            "",
        ]
        for label, path in (
            ("工程分析", sources["analysis"]),
            ("仓库地图", sources["repoMap"]),
            ("测试手册", sources["testPlaybook"]),
            ("风险清单", sources["risks"]),
        ):
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                parts.extend([f"## {label}", text[:8000], ""])
        history = self._prompt_records(sources["prompts"])
        if history:
            parts.extend(["## 最近历史题面", ""])
            for item in history[-10:]:
                parts.extend(
                    [
                        f"### {item.get('taskName') or item.get('taskId')}",
                        f"- 状态：{item.get('jobStatus', '')}；QC：{item.get('qcStatus', '')}；"
                        f"Feedback：{item.get('feedbackScore', '')}",
                        f"- 题面：{_compact(str(item.get('prompt') or ''), MAX_PROMPT_TEXT)}",
                        "",
                    ]
                )
        monitor = task_root / "monitor"
        pack = monitor / "context-pack.md"
        manifest = monitor / "context-sources.json"
        _write_text(pack, "\n".join(parts).rstrip() + "\n")
        _write_json(
            manifest,
            {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "projectId": record["projectId"],
                "engineeringId": record["engineeringId"],
                "sources": {key: str(value) for key, value in sources.items()},
                "promptCount": len(history),
            },
        )
        return {"pack": str(pack), "manifest": str(manifest), "engineering": str(engineering_dir)}

    def _task_record(
        self,
        task: dict[str, Any],
        *,
        project_id: str,
        engineering_id: str,
        ledger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = str(task["id"])
        root = Path(str(task.get("task_root") or ""))
        state = _read_json(root / "monitor" / "state.json", {}) or {}
        submission_data = state.get("submission")
        submission = submission_data if isinstance(submission_data, dict) else {}
        latest_ledger = ledger or {}
        if not latest_ledger:
            rows = self.repo.query(
                "SELECT outcome, failure_kind, project_name, engineering_name FROM usage_ledger "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            latest_ledger = rows[0] if rows else {}
        stage = self.repo.query(
            "SELECT failure_kind, failure_detail FROM autopilot_stages "
            "WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        )
        latest_stage = stage[0] if stage else {}
        stage_failure_kind = str(latest_stage.get("failure_kind") or "")
        prompt = _prompt_text(root)
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest() if prompt else str(task.get("prompt_sha256") or "")
        return {
            "schemaVersion": SCHEMA_VERSION,
            "taskId": task_id,
            "taskName": str(task.get("task_name") or ""),
            "taskRoot": str(root),
            "projectId": project_id,
            "projectName": str(latest_ledger.get("project_name") or ""),
            "engineeringId": engineering_id,
            "engineeringName": str(latest_ledger.get("engineering_name") or task.get("task_name") or ""),
            "domain": str(task.get("domain") or ""),
            "subDirection": str(task.get("sub_direction") or ""),
            "taskType": str(task.get("task_type") or ""),
            "difficulty": str(task.get("difficulty") or ""),
            "jobStatus": str(task.get("job_status") or latest_ledger.get("outcome") or ""),
            "skillStatus": str(task.get("skill_status") or ""),
            "feedbackScore": task.get("feedback_score"),
            "qcStatus": str(submission.get("qcConclusion") or submission.get("status") or ""),
            "outcome": str(latest_ledger.get("outcome") or ""),
            "failureKind": stage_failure_kind or str(latest_ledger.get("failure_kind") or ""),
            "failureDetail": (
                _compact(str(latest_stage.get("failure_detail") or ""), 500)
                if stage_failure_kind
                else ""
            ),
            "promptSha256": prompt_sha,
            "prompt": prompt,
            "initialSha": str(task.get("initial_sha") or state.get("initialSha") or ""),
            "artifactSha": str(task.get("artifact_sha") or (state.get("artifact") or {}).get("sha") or ""),
            "createdAt": str(task.get("created_at") or state.get("createdAt") or ""),
            "updatedAt": str(task.get("updated_at") or ""),
            "exportedAt": str(task.get("exported_at") or ""),
        }

    def _directories(self, task_id: str, project_id: str, engineering_id: str) -> tuple[Path, Path]:
        scope = self.repo.task_account_scope(task_id)
        project = self.root / "accounts" / scope if scope else self.root / _safe(project_id, "unassigned")
        engineering = hashlib.sha256(engineering_id.encode()).hexdigest() if scope else _safe(engineering_id, task_id)
        directory = project / "engineering" / engineering
        if scope and directory.resolve() != self.root.resolve() / directory.relative_to(self.root):
            raise ApiError(409, "TASK_SOURCE_REVIEW", "项目上下文目录与核验账号不一致")
        return project, directory

    def _engineering_records(
        self,
        project_id: str,
        engineering_id: str,
        *,
        fallback: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = self.repo.query(
            "SELECT id FROM tasks WHERE (engineering_id = ? AND coalesce(project_id,'') = ?) OR id = ? ORDER BY created_at",
            (engineering_id, project_id, fallback["taskId"]),
        )
        records: dict[str, dict[str, Any]] = {}
        scope = self.repo.task_account_scope(fallback["taskId"])
        for row in rows:
            if self.repo.task_account_scope(str(row["id"])) != scope:
                continue
            task = self.repo.get_task(str(row["id"]))
            if task:
                record = self._task_record(task, project_id=project_id, engineering_id=engineering_id)
                records[record["taskId"]] = record
        records[fallback["taskId"]] = fallback
        return sorted(records.values(), key=lambda item: str(item.get("createdAt") or ""))

    def _write_project(self, project_dir: Path, project_id: str, records: list[dict[str, Any]]) -> None:
        latest = records[-1] if records else {}
        project_name = str(latest.get("projectName") or project_id or "未关联项目")
        engineering = {str(item.get("engineeringId") or ""): str(item.get("engineeringName") or "") for item in records}
        lines = [
            f"# {project_name}",
            "",
            f"- Project ID：`{project_id or 'unassigned'}`",
            f"- 更新时间：{datetime.now(UTC).replace(microsecond=0).isoformat()}",
            f"- 已记录工程：{len(engineering)}",
            "",
            "## 工程索引",
            "",
        ]
        for engineering_id, name in sorted(engineering.items()):
            _, directory = self._directories(latest["taskId"], project_id, engineering_id)
            path = directory.relative_to(project_dir) / "analysis.md"
            lines.append(f"- [{name or engineering_id}]({path.as_posix()})")
        _write_text(project_dir / "project.md", "\n".join(lines).rstrip() + "\n")

    def _write_engineering(
        self,
        project_dir: Path,
        engineering_dir: Path,
        records: list[dict[str, Any]],
    ) -> None:
        latest = records[-1] if records else {}
        statuses = Counter(str(item.get("jobStatus") or "unknown") for item in records)
        failures = Counter(str(item.get("failureKind") or "") for item in records if item.get("failureKind"))
        qc = Counter(str(item.get("qcStatus") or "unknown") for item in records)
        context = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "projectId": latest.get("projectId", ""),
            "projectName": latest.get("projectName", ""),
            "engineeringId": latest.get("engineeringId", ""),
            "engineeringName": latest.get("engineeringName", ""),
            "latestSnapshotSha": latest.get("artifactSha") or latest.get("initialSha") or "",
            "counts": {
                "tasks": len(records),
                "jobStatuses": dict(statuses),
                "qcStatuses": dict(qc),
                "failureKinds": dict(failures),
            },
            "tasks": records,
        }
        _write_json(engineering_dir / "context.json", context)
        _write_text(engineering_dir / "analysis.md", self._analysis_markdown(context, records))
        _write_text(engineering_dir / "repo-map.md", self._repo_map_markdown(records))
        _write_text(engineering_dir / "test-playbook.md", self._test_playbook_markdown(records))
        _write_json(
            engineering_dir / "risks.json",
            {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAt": context["generatedAt"],
                "failureKinds": dict(failures),
                "latestFailures": [
                    {
                        "taskId": item.get("taskId"),
                        "taskName": item.get("taskName"),
                        "failureKind": item.get("failureKind"),
                        "failureDetail": item.get("failureDetail"),
                    }
                    for item in records
                    if item.get("failureKind")
                ][-20:],
                "knownGaps": [
                    {
                        "taskId": item.get("taskId"),
                        "feedbackScore": item.get("feedbackScore"),
                        "qcStatus": item.get("qcStatus"),
                    }
                    for item in records
                    if item.get("feedbackScore") is not None and int(item.get("feedbackScore") or 0) < 4
                ],
            },
        )
        scripts: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "engineeringId": latest.get("engineeringId", ""),
            "generatedAt": context["generatedAt"],
            "files": {
                "analysis": str(engineering_dir / "analysis.md"),
                "context": str(engineering_dir / "context.json"),
                "repoMap": str(engineering_dir / "repo-map.md"),
                "testPlaybook": str(engineering_dir / "test-playbook.md"),
                "risks": str(engineering_dir / "risks.json"),
                "prompts": str(engineering_dir / "prompts.jsonl"),
            },
        }
        _write_json(engineering_dir / "index.json", scripts)
        _ = project_dir

    def _analysis_markdown(self, context: dict[str, Any], records: list[dict[str, Any]]) -> str:
        lines = [
            f"# {context.get('engineeringName') or context.get('engineeringId')}",
            "",
            "> 该文件由平台从真实任务、提示词和运行证据生成；事实优先于模型自述。",
            "",
            "## 概览",
            "",
            f"- Project：{context.get('projectName') or context.get('projectId')}",
            f"- 最近快照：`{context.get('latestSnapshotSha') or '未知'}`",
            f"- 任务数：{len(records)}",
            f"- 状态：{json.dumps(context['counts']['jobStatuses'], ensure_ascii=False)}",
            f"- QC：{json.dumps(context['counts']['qcStatuses'], ensure_ascii=False)}",
            "",
            "## 历史题面",
            "",
        ]
        for item in records[-20:]:
            lines.append(
                f"- `{item.get('taskId')}` {item.get('taskName')}："
                f"{item.get('jobStatus')} / QC {item.get('qcStatus') or '-'} / Feedback {item.get('feedbackScore') if item.get('feedbackScore') is not None else '-'}；"
                f"{_compact(str(item.get('prompt') or ''), 260)}"
            )
        failure_kinds = Counter(str(item.get("failureKind") or "") for item in records if item.get("failureKind"))
        lines.extend(["", "## 失败模式", ""])
        if failure_kinds:
            lines.extend(f"- {kind or 'unknown'}：{count} 次" for kind, count in failure_kinds.most_common())
        else:
            lines.append("- 暂无失败记录")
        lines.extend(
            [
                "",
                "## 新会话使用要求",
                "",
                "1. 先复用本工程的入口、测试命令和已确认风险，不重复做全仓摸底。",
                "2. 新题必须与历史题面在业务问题、冲突点、关键取舍或状态链路上有实质差异。",
                "3. 若最近快照与当前工作区不一致，只把本文件当线索，重新核对变更文件。",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _repo_map_markdown(self, records: list[dict[str, Any]]) -> str:
        workspace: Path | None = None
        for item in reversed(records):
            candidate = Path(str(item.get("taskRoot") or "")) / "workspace"
            if candidate.is_dir():
                workspace = candidate
                break
        lines = ["# 仓库地图", ""]
        if workspace is None:
            lines.append("未找到可用工作区。")
            return "\n".join(lines) + "\n"
        lines.extend([f"- 工作区：`{workspace}`", "", "## 顶层结构", ""])
        try:
            for child in sorted(workspace.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))[:80]:
                if child.name in SKIP_DIRS:
                    continue
                lines.append(f"- {'目录' if child.is_dir() else '文件'}：`{child.name}`")
        except OSError:
            lines.append("工作区无法读取。")
        suffix_counts: Counter[str] = Counter()
        key_files: list[str] = []
        for current, dirs, files in os.walk(workspace):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in files:
                path = Path(current) / name
                suffix_counts[path.suffix.lower()] += 1
                relative = path.relative_to(workspace).as_posix()
                if len(key_files) < 80 and (
                    path.suffix.lower() in CODE_SUFFIXES
                    and any(token in relative.lower() for token in ("main", "app", "route", "service", "model", "schema", "config"))
                ):
                    key_files.append(relative)
        lines.extend(["", "## 代码分布", ""])
        lines.extend(f"- `{suffix or '(none)'}`：{count}" for suffix, count in suffix_counts.most_common(12))
        lines.extend(["", "## 重点文件候选", ""])
        lines.extend(f"- `{path}`" for path in sorted(key_files)[:60])
        readme = workspace / "README.md"
        if readme.is_file():
            lines.extend(["", "## README 摘要", "", readme.read_text(encoding="utf-8", errors="replace")[:4000]])
        return "\n".join(lines).rstrip() + "\n"

    def _test_playbook_markdown(self, records: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        seen: set[str] = set()
        for item in reversed(records):
            root = Path(str(item.get("taskRoot") or ""))
            verification = _read_json(root / "monitor" / "runtime-verification.json", {}) or {}
            for run in verification.get("runs") or []:
                command = str(run.get("command") or "").strip()
                if not command or command in seen:
                    continue
                seen.add(command)
                rows.append(
                    f"- `{command}`\n"
                    f"  - 用途：{run.get('label') or '未标注'}；退出码：{run.get('exitCode')}；"
                    f"耗时：{run.get('durationSeconds')} 秒；记录：{run.get('log') or ''}"
                )
            plan = _read_json(root / "monitor" / "record-plan.json", {}) or {}
            for command in plan.get("startCommand") or []:
                command = str(command).strip()
                if command and command not in seen:
                    seen.add(command)
                    rows.append(f"- `{command}`\n  - 用途：录屏启动命令；来源：`{root / 'monitor/record-plan.json'}`")
        if not rows:
            rows.append("- 暂无已验证的运行命令；新任务必须先补充真实执行记录。")
        return "# 测试与运行手册\n\n" + "\n".join(rows) + "\n"

    def _append_prompt(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._prompt_records(path)
        if any(str(item.get("taskId") or "") == record["taskId"] for item in existing):
            return
        with path.open("a+", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                known = {
                    str(item.get("taskId") or "")
                    for item in (
                        json.loads(line)
                        for line in handle.read().splitlines()
                        if line.strip()
                    )
                }
                if record["taskId"] not in known:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _prompt_records(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return records
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records
