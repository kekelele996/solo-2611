from __future__ import annotations

from pathlib import Path

from solo_auto.gates.common import GateResult, evaluate_single_step


def feedback_gate(
    task_root: Path,
    *,
    duplicate_sessions: dict[str, int] | None = None,
) -> GateResult:
    return evaluate_single_step(task_root, 10, duplicate_sessions=duplicate_sessions)
