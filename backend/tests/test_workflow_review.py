from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.exceptions import InvalidTag

from solo_auto import account_profile
from solo_auto.gateway.skill import SkillResult
from solo_auto.pipeline.autopilot import Autopilot, Job
from solo_auto.settings import SubmissionSettings


def test_account_backup_round_trip_and_wrong_password() -> None:
    profile = {"solo2": {"username": "test-account", "locked_domain": "数据工程"}}
    assert account_profile.unpack(account_profile.pack(profile, ""), "") == profile
    secret = {**profile, "secrets": {"github_token": "account-backup-test-marker"}}
    encrypted = account_profile.pack(secret, "roundtrip-test")
    assert "account-backup-test-marker" not in json.dumps(encrypted)
    assert account_profile.unpack(encrypted, "roundtrip-test") == secret
    with pytest.raises(InvalidTag):
        account_profile.unpack(encrypted, "incorrect")
    with pytest.raises(ValueError):
        account_profile.unpack({"format": account_profile.FORMAT, "profile": secret}, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,commands,outcome", [
    ({}, ["submit"], "done"),
    ({"status": "quota_deferred", "retryAt": "2026-11-19T00:00:00+08:00"}, ["submit"], "done"),
    ({"submissionId": 2, "status": "submitted"}, ["submission-status"], "done"),
    ({"submissionId": 2, "status": "pending_fix", "currentVersion": 1}, [], "delivering"),
    ({"submissionId": 2, "status": "review_timeout"}, [], "failed"),
])
async def test_workbench_owns_one_submission_watcher(tmp_path: Path, saved: dict, commands: list, outcome: str) -> None:
    root = tmp_path / "task"
    directory = root / "monitor/submission"
    directory.mkdir(parents=True)
    if saved:
        (directory / "api-result.json").write_text(json.dumps(saved))
    pilot = object.__new__(Autopilot)
    pilot.submission = SubmissionSettings()
    pilot.repo = Mock()
    pilot.prompt_history = Mock()
    pilot.ledger = Mock()
    pilot.review_tasks = {}
    pilot._changed = Mock()
    job = Job("id", "题目")
    pilot._finish = lambda job, stage, detail: setattr(job, "stage", stage)
    async def terminate(job: Job, **_: object) -> None:
        job.stage = "failed"
    pilot._terminate_job = terminate
    call = AsyncMock(return_value=SkillResult(code=0, duration=0, data={"submissionId": 2, "status": "qc_passed"}))
    pilot.gateway = SimpleNamespace(call=call)
    await asyncio.gather(*(pilot._complete_or_audit(job, {}, root) for _ in range(2)))
    await asyncio.gather(*pilot.review_tasks.values())
    assert job.stage == outcome
    assert [entry.args[0][0] for entry in call.await_args_list] == commands
    assert not job.awaiting_platform
    if outcome == "delivering":
        job.repair_ready = True
        await pilot._complete_or_audit(job, {}, root)
        await asyncio.gather(*pilot.review_tasks.values())
        assert call.await_args.args[0][0] == "repair"
        assert job.stage == "done"


@pytest.mark.asyncio
async def test_daily_quota_wait_survives_tick_and_resumes_original_task(tmp_path: Path) -> None:
    directory = tmp_path / 'monitor/submission'
    directory.mkdir(parents=True)
    pilot = object.__new__(Autopilot)
    pilot._lock = asyncio.Lock()
    pilot.agents = {}
    pilot.breaker = SimpleNamespace(tripped=False)
    pilot.settings = SimpleNamespace(enabled=True, agent_mode='cli', task_timeout_seconds=7200)
    pilot.paused = False
    pilot.submission = SubmissionSettings()
    pilot.dispatcher = None
    pilot.ledger = None
    pilot.prompt_history = Mock()
    pilot.repo = Mock()
    pilot.repo.get_task.return_value = {'task_root': str(tmp_path)}
    pilot.review_tasks = {}
    pilot._changed = Mock()
    job = Job('original', '项目')
    pilot.jobs = {'original': job}
    deferred = {'status': 'quota_deferred', 'retryAt': '2099-01-01T00:00:00+08:00'}
    pilot.gateway = SimpleNamespace(call=AsyncMock(side_effect=[SkillResult(code=2, duration=0, data=deferred),
        SkillResult(code=0, duration=0, data={'status': 'qc_passed', 'submissionId': 7})]))
    await pilot._complete_or_audit(job, {}, tmp_path)
    await asyncio.gather(*pilot.review_tasks.values())
    assert job.stage == 'waiting_quota'
    (directory / 'api-result.json').write_text(json.dumps(deferred))
    await pilot.tick()
    assert pilot.gateway.call.await_count == 1
    job.quota_retry_at = job.stage_started_at = job.started_at = '2026-01-01T00:00:00+08:00'
    # 替换完成后的通知，保留实际换日、超时核对与提交处理。
    pilot._finish = lambda j, stage, detail: setattr(j, 'stage', stage)
    await pilot.tick()
    await asyncio.gather(*pilot.review_tasks.values())
    assert job.stage == 'done'
    commands = [call.args[0] for call in pilot.gateway.call.await_args_list]
    assert len(commands) == 2 and commands[0] == commands[1] and commands[1][:3] == ['submit', '--task-root', str(tmp_path)]
