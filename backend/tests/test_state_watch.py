from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from solo_auto.store.state_watch import StateWatcher


@pytest.mark.asyncio
async def test_state_watcher_debounces_rapid_writes(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    state_path = tasks_dir / "task" / "monitor" / "state.json"
    state_path.parent.mkdir(parents=True)
    watcher = StateWatcher(tasks_dir)
    watcher.start(asyncio.get_running_loop())
    calls = 0
    changed = asyncio.Event()

    async def callback() -> None:
        nonlocal calls
        calls += 1
        changed.set()

    consumer = asyncio.create_task(watcher.changes(callback))
    try:
        await asyncio.sleep(0.2)
        for number in range(5):
            state_path.write_text(f'{{"n": {number}}}', encoding="utf-8")
        await asyncio.wait_for(changed.wait(), timeout=3)
        await asyncio.sleep(0.25)
        assert calls == 1
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        watcher.stop()
