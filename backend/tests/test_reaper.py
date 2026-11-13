from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from solo_auto.pipeline.reaper import ProcessRow, find_task_processes, reap_task_processes

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process tree")

FAKE_SKILL = """
import os, signal, subprocess, sys, time
args = sys.argv[1:]
if "--stubborn" in args:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pid_file = args[args.index("--pid-file") + 1]
with open(pid_file + ".tmp", "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
os.replace(pid_file + ".tmp", pid_file)
time.sleep(120)
"""

DETACH = (
    "import subprocess, sys; "
    "subprocess.Popen(sys.argv[1:], start_new_session=True, "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 已退出但还没被 launchd/init 回收的僵尸进程也算结束。
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[object]:
    """Start a fake skill run that is NOT a descendant of the test process, like a ChatGPT session's run."""
    script = tmp_path / "skill" / "scripts" / "solo2611.py"
    script.parent.mkdir(parents=True)
    script.write_text(FAKE_SKILL, encoding="utf-8")
    started: list[int] = []

    def start(root: Path, *extra: str, detached: bool = True) -> tuple[int, int]:
        pid_file = tmp_path / f"pids-{len(started)}"
        argv = [sys.executable, str(script), "run", "--task-root", str(root), *extra, "--pid-file", str(pid_file)]
        if detached:
            subprocess.run([sys.executable, "-c", DETACH, *argv], check=True)
        else:
            started.append(subprocess.Popen(argv).pid)
        deadline = time.monotonic() + 10
        while not pid_file.exists():
            assert time.monotonic() < deadline, "fake skill did not start"
            time.sleep(0.05)
        parent, child = (int(item) for item in pid_file.read_text().split())
        started.extend([parent, child])
        return parent, child

    yield start
    for pid in started:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def test_leftover_run_and_its_children_are_ended(tmp_path: Path, spawn: object) -> None:
    root = tmp_path / "tasks" / "2026-11-13-客户流失预测评分卡-vq5p"
    root.mkdir(parents=True)
    neighbour = tmp_path / "tasks" / "2026-11-13-客户流失预测评分卡-vq5p2"
    neighbour.mkdir(parents=True)
    run, child = spawn(root)  # type: ignore[operator]
    other_run, other_child = spawn(neighbour)  # type: ignore[operator]
    own_run, own_child = spawn(root, detached=False)  # type: ignore[operator]

    found = {row.pid for row in find_task_processes(root)}
    assert {run, child} <= found
    assert not found & {other_run, other_child, own_run, own_child}

    reaped = reap_task_processes(root, grace_seconds=3)
    assert {item["pid"] for item in reaped} == found
    deadline = time.monotonic() + 3
    while (_alive(run) or _alive(child)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(run) and not _alive(child)
    # 前缀相同的别的任务、以及本服务自己拉起的运行都不能被误杀。
    assert all(_alive(pid) for pid in (other_run, other_child, own_run, own_child))
    assert find_task_processes(root) == []


def test_processes_that_ignore_sigterm_are_killed_after_grace(tmp_path: Path, spawn: object) -> None:
    root = tmp_path / "tasks" / "2026-11-13-会员-RFM-n3wy"
    root.mkdir(parents=True)
    run, _child = spawn(root, "--stubborn")  # type: ignore[operator]

    reaped = reap_task_processes(root, grace_seconds=0.5)
    by_pid = {item["pid"]: item["signal"] for item in reaped}
    assert by_pid[run] == "KILL"
    deadline = time.monotonic() + 3
    while _alive(run) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(run)


def test_only_skill_commands_seed_the_match() -> None:
    root = Path("/runs/solo-2611/2026-11-13-task-ab")
    rows = [
        ProcessRow(10, 1, f"python3 scripts/solo2611.py run --task-root {root} --attempts 3"),
        ProcessRow(11, 10, "docker run -i --name solo2611-task-x"),
        ProcessRow(12, 1, f"vim {root}/workspace/app.py"),
        ProcessRow(13, 1, f"python3 scripts/solo2611.py run --task-root {root}c"),
        ProcessRow(14, 1, f"python3 scripts/solo2611.py run --task-root '{root}'"),
    ]
    assert [row.pid for row in find_task_processes(root, rows)] == [10, 11, 14]
