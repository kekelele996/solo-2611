from __future__ import annotations

from pathlib import Path

from solo_auto.gates.common import GateResult, evaluate_single_step


def prompt_gate(task_root: Path) -> GateResult:
    return evaluate_single_step(task_root, 3)
