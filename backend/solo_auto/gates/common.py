"""Gate result shared by prompt, feedback and export checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solo_auto.pipeline.steps import Step, evaluate_steps


@dataclass(slots=True)
class GateResult:
    ok: bool
    step: Step
    errors: list[str]


def evaluate_single_step(
    task_root: Path,
    number: int,
    *,
    duplicate_sessions: dict[str, int] | None = None,
) -> GateResult:
    evaluation = evaluate_steps(task_root, duplicate_sessions=duplicate_sessions)
    step = next(item for item in evaluation.steps if item.number == number)
    return GateResult(ok=step.ok, step=step, errors=[] if step.ok else [step.detail or step.name])
