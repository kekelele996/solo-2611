"""Evidence-based failure classifier."""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Evidence:
    stage: str
    exit_code: int | None = None
    skill_json: dict[str, Any] = field(default_factory=dict)
    skill_stderr: str = ""
    container: dict[str, Any] = field(default_factory=dict)
    trace_tail: str = ""
    gate_errors: list[str] = field(default_factory=list)
    agent_verdict: str = ""
    rollout_terminal: str = ""
    terminated_by: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def searchable(self) -> dict[str, str]:
        return {
            "skill_stderr": self.skill_stderr,
            "trace_tail": self.trace_tail,
            "gate_errors": "\n".join(self.gate_errors),
            "agent_verdict": self.agent_verdict,
            "rollout_terminal": self.rollout_terminal,
        }


@dataclass(slots=True)
class Failure:
    kind: str
    transient: bool
    charge: bool
    detail: str
    matched_by: str
    evidence: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 0
    backoff_multiplier: float = 1.0
    scope: str = "task"
    action: str = "retry"
    disposition: str = "retry"
    hold_source: str = ""
    hold_scope: list[str] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return asdict(self)

    def signature(self) -> str:
        payload = "\n".join(
            [
                self.kind,
                self.detail,
                *sorted(str(item) for item in self.evidence.get("gate_errors") or []),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Pattern:
    kind: str
    where: list[str]
    regex: re.Pattern[str]
    source: str


class FailureClassifier:
    def __init__(self, home: Path) -> None:
        self.path = home / "dispatch" / "patterns.toml"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            shutil.copy2(Path(__file__).with_name("default_patterns.toml"), self.path)
        self._mtime = 0.0
        self._patterns: list[Pattern] = []
        self._warnings: list[str] = []
        self._load()

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def classify(self, evidence: Evidence) -> Failure:
        if evidence.terminated_by:
            kind = {
                "user": "control.interrupted",
                "restart": "control.restart",
            }.get(evidence.terminated_by, f"guard.{evidence.terminated_by.removeprefix('guard:')}")
            return _failure(kind, evidence, matched_by="terminated_by")
        context_kind = str(evidence.context.get("failure_kind") or "")
        if context_kind:
            return _failure(context_kind, evidence, matched_by="context")
        structured = evidence.skill_json
        structured_kind = str(
            structured.get("failure_kind")
            or structured.get("failureKind")
            or structured.get("kind")
            or ""
        )
        if structured_kind in DEFAULTS:
            return _failure(structured_kind, evidence, matched_by="skill_json")
        if evidence.container.get("oom_killed") or evidence.container.get("OOMKilled"):
            return _failure("container.oom", evidence, matched_by="container")
        if evidence.container.get("exit_code") not in {None, 0}:
            return _failure("container.exited", evidence, matched_by="container")
        if evidence.gate_errors:
            return _failure("gate.rejected", evidence, matched_by="gate_errors")
        if "AUTOPILOT: BLOCKED" in evidence.agent_verdict:
            return _failure("agent.blocked", evidence, matched_by="agent_verdict")
        if "AUTOPILOT: FAILED" in evidence.agent_verdict:
            return _failure("agent.reported", evidence, matched_by="agent_verdict")
        self._reload_if_changed()
        sources = evidence.searchable()
        for pattern in self._patterns:
            for where in pattern.where:
                text = sources.get(where, "")
                if text and pattern.regex.search(text):
                    return _failure(
                        pattern.kind,
                        evidence,
                        matched_by=f"pattern:{pattern.source}:{pattern.kind}",
                    )
        if evidence.stage == "running" and evidence.exit_code == 0:
            return _failure("run.incomplete", evidence, matched_by="stage_fallback")
        return _failure(f"unknown.{evidence.stage or 'stage'}", evidence, matched_by="fallback")

    def _load(self) -> None:
        self._warnings = []
        try:
            self._mtime = self.path.stat().st_mtime
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            self._warnings.append(f"patterns.toml 无法读取：{exc}")
            self._patterns = []
            return
        patterns: list[Pattern] = []
        for index, raw in enumerate(data.get("pattern") or [], 1):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "")
            where = [str(item) for item in raw.get("where") or []]
            try:
                regex = re.compile(str(raw.get("regex") or ""))
            except re.error as exc:
                self._warnings.append(f"第 {index} 条正则无效：{exc}")
                continue
            if not kind or not where:
                self._warnings.append(f"第 {index} 条缺少 kind 或 where")
                continue
            patterns.append(Pattern(kind, where, regex, str(index)))
        self._patterns = patterns

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self._load()


DEFAULTS: dict[str, dict[str, Any]] = {
    "control.interrupted": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "interrupted"},
    "control.restart": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "disposition": "requeue"},
    "gateway.unreachable": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "llm_guard", "scope": ["containers"]},
    "gateway.rate_limited": {"transient": True, "charge": False, "max": 3, "multiplier": 1, "disposition": "retry"},
    "gateway.timeout": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "gateway.auth": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "breaker", "scope": ["global"]},
    "gateway.quota": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "breaker", "scope": ["containers"]},
    "gateway.model_mismatch": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "breaker", "scope": ["containers"]},
    "network.offline": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "llm_guard", "scope": ["containers"]},
    "docker.unavailable": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "docker_guard", "scope": ["containers", "recording"]},
    "container.start_failed": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "container.image": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "skill_guard", "scope": ["containers"]},
    "container.oom": {"transient": False, "charge": True, "max": 1, "multiplier": 3},
    "container.exited": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "run.timeout": {"transient": False, "charge": True, "max": 1, "multiplier": 1},
    "run.no_progress": {"transient": True, "charge": True, "max": 1, "multiplier": 1},
    "run.incomplete": {"transient": False, "charge": True, "max": 2, "multiplier": 1},
    "run.interrupted": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "disposition": "requeue"},
    "agent.missing": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "session_guard", "scope": ["sessions"]},
    "agent.launch_lost": {"transient": True, "charge": False, "max": 2, "multiplier": 0.5},
    "agent.timeout": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "agent.no_progress": {"transient": True, "charge": True, "max": 1, "multiplier": 1},
    "agent.blocked": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "manual"},
    "agent.reported": {"transient": False, "charge": True, "max": 2, "multiplier": 1},
    "agent.aborted": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "agent.usage_limited": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "session_guard", "scope": ["sessions"]},
    "gate.rejected": {"transient": False, "charge": True, "max": 2, "multiplier": 1},
    "gate.duplicate_session": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "quarantine"},
    "recording.permission": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "recording_guard", "scope": ["recording"]},
    "recording.failed": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "recording.privacy": {"transient": False, "charge": True, "max": 1, "multiplier": 1},
    "github.transient": {"transient": True, "charge": True, "max": 3, "multiplier": 1},
    "github.auth": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "hold": "breaker", "scope": ["global"]},
    "github.rate_limited": {"transient": True, "charge": False, "max": 3, "multiplier": 1},
    "export.failed": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "submit.failed": {"transient": True, "charge": True, "max": 3, "multiplier": 2},
    "submit.rejected": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "manual"},
    "source.auth": {"transient": True, "charge": False, "max": 1, "multiplier": 0, "hold": "source_guard", "scope": ["intake"]},
    "source.conflict": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "skip"},
    "source.unavailable": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "source_guard", "scope": ["intake"]},
    "source.download": {"transient": True, "charge": True, "max": 2, "multiplier": 1},
    "disk.low": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "disk_guard", "scope": ["containers", "intake"]},
    "disk.full": {"transient": True, "charge": False, "max": 0, "multiplier": 0, "hold": "disk_guard", "scope": ["global"]},
    "state.incompatible": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "quarantine"},
    "state.missing": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "missing"},
    "retry.exhausted": {"transient": False, "charge": False, "max": 0, "multiplier": 0, "disposition": "quarantine"},
}


def _failure(kind: str, evidence: Evidence, *, matched_by: str) -> Failure:
    spec = DEFAULTS.get(kind, {})
    detail = (
        evidence.agent_verdict
        or evidence.skill_stderr.strip()
        or "\n".join(evidence.gate_errors).strip()
        or str(evidence.container.get("error") or "")
        or evidence.rollout_terminal
        or f"exit {evidence.exit_code}"
    )[-4000:]
    scope = list(spec.get("scope") or ["task"])
    action = str(spec.get("action") or ("hold" if spec.get("hold") else "retry"))
    disposition = str(spec.get("disposition") or ("hold" if action == "hold" else "retry"))
    return Failure(
        kind=kind,
        transient=bool(spec.get("transient", False)),
        charge=bool(spec.get("charge", True)),
        detail=detail,
        matched_by=matched_by,
        evidence={
            "stage": evidence.stage,
            "exit_code": evidence.exit_code,
            "gate_errors": evidence.gate_errors,
            "container": evidence.container,
        },
        max_retries=int(spec.get("max") or 0),
        backoff_multiplier=float(spec.get("multiplier") or 1),
        scope=scope[0],
        action=action,
        disposition=disposition,
        hold_source=str(spec.get("hold") or ""),
        hold_scope=scope,
    )
