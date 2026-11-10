from __future__ import annotations

from pathlib import Path

from solo_auto.gates.common import GateResult, evaluate_single_step
from solo_auto.pipeline.steps import Evaluation, evaluate_steps


def export_gate(
    task_root: Path,
    *,
    duplicate_sessions: dict[str, int] | None = None,
) -> GateResult:
    return evaluate_single_step(task_root, 11, duplicate_sessions=duplicate_sessions)


def all_completion_gates(
    task_root: Path,
    *,
    duplicate_sessions: dict[str, int] | None = None,
) -> Evaluation:
    return evaluate_steps(task_root, duplicate_sessions=duplicate_sessions)
