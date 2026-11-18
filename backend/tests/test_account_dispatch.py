from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import subprocess
import sys
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from solo_auto.account_binding import standby_key
from solo_auto.api.dispatch import snapshot as dispatch_snapshot
from solo_auto.app import create_app
from solo_auto.context import AppContext
from solo_auto.errors import ApiError
from solo_auto.gateway.skill import SkillResult
from solo_auto.pipeline import account_dispatch, autopilot
from solo_auto.pipeline.account_dispatch import EARLY_QUOTA, ROLLOVER, SubmissionCount
from solo_auto.pipeline.autopilot import Job
from solo_auto.pipeline.scheduler import QueueEntry
from solo_auto.sources import github
from solo_auto.sources.solo2 import SHANGHAI, DailySubmissions


@pytest.fixture
def accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppContext, Any]:
    clock = SimpleNamespace(now=datetime(2026, 11, 18, 19, 59, 59, tzinfo=SHANGHAI))
    monkeypatch.setattr(account_dispatch, 'shanghai_now', lambda: clock.now)
    monkeypatch.setenv('SOLO_AUTO_TASKS_DIR', str(tmp_path / 'tasks'))
    app = create_app(tmp_path / 'workbench', auto_start=False)
    ctx = app.state.context
    ctx.settings.solo2.username = 'main'
    ctx.settings.solo2.locked_domain = '数据工程'
    ctx.settings.source.username = 'main-source'
    ctx.settings.submission.auto_submit = True
    ctx.settings.autopilot.agent_mode = 'cli'
    ctx.autopilot.paused = False
    ctx.secrets.set_json(standby_key(ctx.settings.solo2), {'username': 'reserve', 'domain': '安全', 'password': 'fixture'})
    ctx.secrets.set_json(ctx.routing.secret_key, {
        'primary_project_id': 'pool-a', 'primary_project_name': '主号项目', 'primary_github_owner': 'main-gh',
        'primary_source_user': 'main-source', 'primary_source_address': ctx.source.settings.base_url,
        'standby_project_id': 'pool-b', 'standby_project_name': '副号项目', 'standby_github_owner': 'reserve-gh',
        'standby_account': 'reserve', 'standby_source_user': 'other-source', 'standby_source_password': 'fixture',
        'standby_source_address': ctx.source.settings.base_url,
    })
    for role in ('primary', 'standby'):
        lane = ctx.routing.lane(role)
        monkeypatch.setattr(lane.source, 'verify_identity', AsyncMock())
        monkeypatch.setattr(lane.source, 'release', AsyncMock(return_value={'ok': True}))
    return ctx, clock


def _json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def _stats(ctx: AppContext, role: str, now: datetime, count: int) -> None:
    server, account = ctx.settings.solo2.base_url, ctx.routing.account(role)[0]
    ctx.routing._stats[(account_dispatch.account_key(server, account), now.date().isoformat())] = SubmissionCount(
        now, DailySubmissions({str(i) for i in range(count)})
    )


def _claim(ctx: AppContext, role: str, name: str, *, staged: bool = False) -> tuple[Path, str]:
    lane = ctx.routing.lane(role)
    assert lane is not None
    meta = ctx.routing.register_claim(lane, name, f'claim-{name}', {'name': name}, day=account_dispatch.shanghai_now().date().isoformat())
    if staged:
        _json(Path(meta['stagingPath']) / 'meta.json', meta)
        return Path(meta['stagingPath']), meta['claimKey']
    root = (ctx.settings.tasks_dir / name).resolve()
    task_id = ctx.repo.task_id_for_root(root)
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    subprocess.run(['git', '-C', str(workspace), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', name], check=True)
    initial = subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip()
    meta.update({"taskRoot": str(root), "initialSha": initial, "zipSha256": 'archive-sha'})
    ctx.routing.save_claim(meta, task_id)
    ctx.repo.upsert_task({'id': task_id, 'task_root': str(root), 'task_name': name, 'job_status': 'authored',
                          'source_kind': 'project_hub', 'skill_status': 'prepared', 'domain': lane.route['domain'], 'sub_direction': '',
                          'engineering_id': name, 'project_id': meta['projectId'], 'claim_id': meta['claimKey'], 'initial_sha': initial})
    _json(root / 'monitor/state.json', {'schema': 'solo-2611/1', 'taskRoot': str(root), 'workspace': str(workspace),
                                       'initialSha': initial, 'accountClaim': meta['claimKey'], 'accountRoute': lane.route,
                                       'fields': {'所属领域': lane.route['domain']}})
    _json(root / 'monitor/workbench.json', {key: meta[key] for key in ('accountRoute', 'projectId', 'engineeringId',
                                                                      'claimKey', 'zipSha256', 'initialSha')})
    return root, task_id


@pytest.mark.asyncio
async def test_estimate_counts_staged_and_new_day_returns_priority(accounts: tuple[AppContext, Any]) -> None:
    ctx, clock = accounts
    _stats(ctx, 'primary', clock.now, 64)
    _stats(ctx, 'standby', clock.now, 39)
    for index in range(5):
        _claim(ctx, 'primary', f'main-{index}')
    assert ctx.routing.selection()[0] == 'primary'
    _, staged = _claim(ctx, 'primary', 'reserved-last', staged=True)
    assert ctx.routing.quota('primary')['reserved'] == 6
    assert ctx.routing.selection()[0] == 'standby'
    assert ctx.routing.selection(include_staged=True)[0] == 'primary'
    assert ctx.routing.staged('primary')['claimKey'] == staged
    ctx.settings.submission.daily_new_limit = 71
    assert ctx.routing.selection()[0] == 'primary'
    ctx.settings.submission.daily_new_limit = 70

    _, unstarted = _claim(ctx, 'standby', 'unstarted')
    root, started = _claim(ctx, 'standby', 'started')
    ctx.routing.audit(started, root, starting=True)
    clock.now = datetime(2026, 11, 19, tzinfo=SHANGHAI)
    _stats(ctx, 'primary', clock.now, 0)
    await ctx.routing.maintain()
    assert ctx.routing.selection()[0] == 'primary'
    assert ctx.routing.row(task_id=started)['claim_state'] == 'held'
    stopped = ctx.routing.row(task_id=unstarted)
    assert (stopped['claim_state'], stopped['drop_reason']) == ('released', ROLLOVER)
    ctx.routing.lane('standby').source.release.assert_awaited_once_with('pool-b', 'unstarted', 'claim-unstarted')
    assert root.exists() and Path(stopped['meta']['taskRoot']).exists()


def test_pool_view_returns_saved_standby_source_credentials(accounts: tuple[AppContext, Any]) -> None:
    ctx, _ = accounts
    view = ctx.routing.pool_view()
    assert view['standby_source_user'] == 'other-source'
    assert view['standby_source_password'] == 'fixture'
    assert view['primary_source_address'] == ctx.source.settings.base_url


@pytest.mark.asyncio
async def test_standby_pool_uses_distinct_group_and_preserves_primary_plan(
    accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _ = accounts
    ctx.secrets.set_json(ctx.routing.secret_key, {})
    await ctx.routing.invalidate()
    ctx.secrets.set('github.token', 'primary-token')
    ctx.secrets.set_json(standby_key(ctx.settings.solo2), {
        'username': 'reserve', 'domain': '安全', 'password': 'fixture', 'github_token': 'standby-token',
    })
    plan = {'id': 'existing', 'status': 'paused', 'project_id': 'pool-a', 'project_name': '主号项目'}
    ctx.intake.plan = dict(plan)
    listed = AsyncMock(return_value=[{'id': 'pool-a', 'name': '主号项目'}, {'id': 'pool-b', 'name': '副号项目'}])
    monkeypatch.setattr(ctx.source, 'list_projects', listed)
    monkeypatch.setattr(github, 'test_credentials', AsyncMock(side_effect=lambda token: {
        'ok': True, 'message': 'ok', 'detail': {'account': token},
    }))
    app = create_app(ctx.paths.home / 'api-test', auto_start=False)
    app.state.context = ctx
    root = '/api/system/accounts'
    endpoint = root + '/profiles/default/engineering-pools'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        await client.put(root + '/view/code', json={'new': 'fixture-code'})
        unlocked = await client.post(root + '/view/unlock', json={'code': 'fixture-code', 'purpose': 'settings'})
        headers = {'X-Account-View': unlocked.json()['token']}
        draft = {'standby_use_primary_source': True, 'primary_project_id': 'pool-a', 'standby_project_id': 'pool-b'}
        assert (await client.put(endpoint, json=draft)).status_code == 403
        stale = await client.put(endpoint, json={**draft, 'primary_project_id': 'old-plan'}, headers=headers)
        assert stale.status_code == 409
        overlap = await client.put(endpoint, json={**draft, 'standby_project_id': 'pool-a'}, headers=headers)
        assert overlap.status_code == 422 and overlap.json()['error']['code'] == 'POOL_PROJECT_OVERLAP'
        before = (await client.get('/api/intake')).json()
        assert before['engineering_pool']['project_id'] == 'pool-a' and not before['standby_engineering_pool']
        options = await client.post(endpoint + '/projects', json=draft, headers=headers)
        assert options.json()['items'] == [{'id': 'pool-b', 'name': '副号项目'}]
        saved = await client.put(endpoint, json=draft, headers=headers)
        assert saved.status_code == 200 and saved.json()['standby_project_id'] == 'pool-b'
        assert (await client.post('/api/intake/project', json={'project_id': 'pool-b'})).status_code == 422
        current = (await client.get('/api/intake')).json()
        assert current['plan'] == plan and current['standby_engineering_pool']['project_id'] == 'pool-b'
    assert not ctx.routing.config.get('primary_project_id')
    standby = ctx.routing.lane('standby')
    assert standby.source is ctx.source and standby.route['githubOwner'] == 'standby-token'
    await ctx.routing.verify_primary_github()
    assert ctx.routing.lane('primary').route['githubOwner'] == 'primary-token'


@pytest.mark.asyncio
async def test_live_count_changes_positions_and_discarded_records_free_capacity(
    accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, clock = accounts
    _stats(ctx, 'standby', clock.now, 39)
    task_ids = []
    for number in range(6):
        _, task_id = _claim(ctx, 'primary', f'engineering-{number}')
        ctx.dispatcher.queue.enqueue(task_id, stage='running')
        task_ids.append(task_id)
    running = task_ids.pop()
    ctx.dispatcher.queue.update(running, status='active')
    ctx.dispatcher.queue.move_waiting(task_ids[-1], 'top')
    ordered = [running, task_ids[-1], *task_ids[:-1]]

    submission_ids = {str(i) for i in range(65)}
    read = AsyncMock(side_effect=[
        DailySubmissions(submission_ids - {'64'}), DailySubmissions(submission_ids),
        DailySubmissions(submission_ids, {'0', '1'}), httpx.ConnectError('offline'),
    ])
    monkeypatch.setattr(ctx.solo2, 'first_submissions', read)
    account_id = account_dispatch.account_key(ctx.settings.solo2.base_url, 'main')
    quota_path = Path(ctx.routing.quota('primary')['quota_directory'])
    quota_path.mkdir(parents=True, exist_ok=True)
    receipts = quota_path / f'receipts-{clock.now.date()}-{account_id}.json'
    receipts.write_text('["0", "900"]', encoding='utf-8')

    for count, discarded, selected in ((64, 0, 'standby'), (65, 0, 'standby'), (63, 2, 'primary')):
        clock.now += timedelta(minutes=1)
        _stats(ctx, 'standby', clock.now, 39)
        await ctx.routing.refresh('primary', force=True)
        assert read.await_args.kwargs['local_ids'] == {'0', '900'}
        snapshot = await dispatch_snapshot(compact=True, metrics=False, ctx=ctx)
        quota = snapshot['submission_quota']['primary']
        assert (quota['submitted'], quota['discarded'], quota['reserved']) == (count, discarded, 6)
        assert snapshot['submission_quota']['selected'] == selected
        assert {row['task_id']: row['estimated_submission'] for row in snapshot['queue']} == dict(
            zip(ordered, range(count + 1, count + 7), strict=True)
        )
    assert ctx.bus.replay(0)[-1].event == 'dispatch.quota'

    receipts.write_text('["0", "900", "901"]', encoding='utf-8')
    assert ctx.routing.quota('primary')['submitted'] == 64
    assert ctx.routing.selection()[0] == 'standby'
    await ctx.routing.refresh('primary', force=True)
    assert not ctx.routing.quota('primary')['fresh']
    assert ctx.routing.quota('primary')['submitted'] == 64
    assert ctx.routing.selection()[0] == ''


@pytest.mark.asyncio
async def test_background_poll_runs_every_minute_and_at_beijing_midnight_without_a_plan(
    accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, clock = accounts
    ctx.routing.config.clear()
    ctx.autopilot.paused = True
    assert ctx.intake.plan is None and not ctx.routing.enabled
    clock.now = clock.now.replace(hour=23, minute=58, second=45)
    started = clock.now
    attempts: list[tuple[datetime, str]] = []
    delays: list[float] = []

    async def read(_client: Any, day: Any, **kwargs: Any) -> DailySubmissions:
        if _client is ctx.solo2:
            attempts.append((clock.now, day.isoformat()))
            clock.now += timedelta(seconds=4)
        return DailySubmissions({day.isoformat()})

    async def wait(delay: float) -> None:
        if len(attempts) == 4:
            raise asyncio.CancelledError
        delays.append(delay)
        clock.now += timedelta(seconds=delay)

    monkeypatch.setattr(account_dispatch.Solo2Client, 'first_submissions', read)
    monkeypatch.setattr(account_dispatch.asyncio, 'sleep', wait)
    ctx.routing.start()
    with pytest.raises(asyncio.CancelledError):
        await ctx.routing._refresh_task
    assert attempts == [
        (started, '2026-11-18'), (started + timedelta(minutes=1), '2026-11-18'),
        (started + timedelta(seconds=75), '2026-11-19'), (started + timedelta(seconds=135), '2026-11-19'),
    ]
    assert delays == [56, 11, 56]
    assert ctx.routing.quota('primary')['submitted'] == 1
    assert ctx.routing.quota('primary')['fresh']


@pytest.mark.asyncio
async def test_early_rejection_restores_from_disk_and_retries_cleanup(accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, clock = accounts
    ready, task_id = _claim(ctx, 'primary', 'in-flight')
    submitted, submitted_id = _claim(ctx, 'primary', 'submitted')
    _, reserve_id = _claim(ctx, 'standby', 'other-account')
    _, staged = _claim(ctx, 'primary', 'staged', staged=True)
    ctx.routing.audit(task_id, ready, starting=True)
    ctx.autopilot.jobs[task_id] = Job(task_id, 'in-flight', stage='chatgpt', desktop_running=True)
    ctx.scheduler.queue.append(QueueEntry(task_id, 'in-flight', ready))
    ctx.dispatcher.queue.enqueue(task_id)
    monkeypatch.setattr(ctx.autopilot, 'refresh_live_sessions', AsyncMock())
    stop = AsyncMock(side_effect=[(False, '等待回合停止'), (True, '已停止')])
    monkeypatch.setattr(ctx.autopilot, '_stop_owned_desktop', stop)
    monkeypatch.setattr(account_dispatch, 'reap_and_record', AsyncMock(return_value=[]))
    cleanup = AsyncMock(return_value=SkillResult(code=0, duration=0))
    monkeypatch.setattr(ctx.gateway, 'call', cleanup)
    lane = ctx.routing.lane('primary')

    async def release(project_id: str, engineering_id: str, claim_id: str) -> dict[str, bool]:
        assert project_id == 'pool-a'
        if engineering_id == 'in-flight' and not released:
            released.append(claim_id)
            raise ApiError(503, 'TEMPORARY', '归还连接暂时不可用')
        return {'ok': True}

    released: list[str] = []
    monkeypatch.setattr(lane.source, 'release', AsyncMock(side_effect=release))
    quota_dir = Path(ctx.routing.quota('primary')['quota_directory'])
    marker = quota_dir / clock.now.date().isoformat()
    _json(marker, {'reason': 'platform', 'observedAt': clock.now.isoformat()})
    _stats(ctx, 'standby', clock.now, 39)
    assert ctx.routing.selection()[0] == 'standby'
    with (quota_dir / 'create.lock').open('wb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        await ctx.routing.maintain()
        assert ctx.routing.row(task_id=task_id)['dropped_at'] == ''
        _json(submitted / 'monitor/submission/api-result.json', {'status': 'qc_running', 'submissionId': 12})
    # 模拟工作台错过通知后跨零点恢复。日期文件仍决定原任务的处置。
    ctx.repo.execute('DELETE FROM dispatch_quota_incidents')
    clock.now = datetime(2026, 11, 19, tzinfo=SHANGHAI)
    await ctx.routing.maintain()
    assert ctx.routing.row(task_id=task_id)['claim_state'] == 'stopping'
    assert ctx.routing.row(claim_key=staged)['claim_state'] == 'released'
    assert ctx.routing.row(task_id=submitted_id)['dropped_at'] == ''
    assert ctx.routing.row(task_id=reserve_id)['drop_reason'] == ROLLOVER
    assert ctx.scheduler.queue_position(task_id) is None
    clock.now += timedelta(seconds=16)
    await ctx.routing.maintain()
    assert ctx.routing.row(task_id=task_id)['claim_state'] == 'return_pending'
    assert ctx.repo.get_task(task_id)['job_status'] == 'archived'
    clock.now += timedelta(seconds=16)
    await ctx.routing.maintain()
    saved = ctx.routing.row(task_id=task_id)
    assert (saved['drop_reason'], saved['claim_state']) == (EARLY_QUOTA, 'released')
    assert stop.await_count == 2 and ready.exists()
    assert '--preserve-traces' in cleanup.await_args.args[0]
    assert ctx.routing.deferred_tasks() == []


@pytest.mark.asyncio
async def test_at_20_multiple_manual_or_restarted_jobs_resume_on_primary(accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, clock = accounts
    clock.now = clock.now.replace(hour=20, minute=0, second=0)

    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock.now.astimezone(tz)

    monkeypatch.setattr(autopilot, 'datetime', FrozenTime)
    tasks = [_claim(ctx, 'primary', name) for name in ('manual', 'restarted')]
    ctx.autopilot.jobs[tasks[1][1]] = Job(tasks[1][1], 'restarted', stage='reviewing')
    retry = '2026-11-19T00:00:00+08:00'
    server = account_dispatch.origin(ctx.settings.solo2.base_url)
    for root, _ in tasks:
        _json(root / 'monitor/submission/quota-deferred.json', {'status': 'quota_deferred', 'retryAt': retry,
              'account': 'main', 'server': server, 'profile': ctx.settings.active_profile})
    ctx.routing.notify_deferred('primary', clock.now, account='main', server=server)
    await ctx.routing.maintain()
    assert len(ctx.routing.quota('primary')['deferred']) == 2
    assert ctx.routing.quota('primary')['reserved'] == 0
    assert ctx.intake._counts({'task_ids': [task_id for _, task_id in tasks]})['in_hand'] == 0
    assert all(ctx.routing.row(task_id=task_id)['dropped_at'] == '' for _, task_id in tasks)

    async def submit(command: list[str], **kwargs: Any) -> SkillResult:
        assert command[:2] == ['submit', '--task-root']
        root = Path(command[2])
        result = {'status': 'qc_passed', 'submissionId': 90 + len(calls), 'account': 'main', 'firstSubmittedAt': clock.now.isoformat()}
        calls.append(root)
        _json(root / 'monitor/submission/api-result.json', result)
        return SkillResult(code=0, duration=0, data=result)

    calls: list[Path] = []
    monkeypatch.setattr(ctx.gateway, 'call', submit)
    await ctx.autopilot.tick()
    assert all(ctx.autopilot.jobs[task_id].stage == 'waiting_quota' for _, task_id in tasks)
    assert calls == []
    clock.now = datetime.fromisoformat(retry)
    _stats(ctx, 'primary', clock.now, 0)
    assert ctx.routing.quota('primary', include_positions=True)['estimated_positions'] == {
        task_id: index for index, (_, task_id) in enumerate(tasks, 1)
    }
    await ctx.autopilot.tick()
    await asyncio.gather(*ctx.autopilot.review_tasks.values())
    assert set(calls) == {root for root, _ in tasks}
    assert all(ctx.autopilot.jobs[task_id].stage == 'done' for _, task_id in tasks)
    assert ctx.routing.deferred_tasks() == []


def test_audit_rejects_another_pool_and_its_workspace(accounts: tuple[AppContext, Any]) -> None:
    ctx, _ = accounts
    root_a, a = _claim(ctx, 'primary', 'a')
    root_b, b = _claim(ctx, 'standby', 'b')
    ctx.routing.audit(a, root_a)
    ctx.routing.audit(b, root_b)
    assert ctx.repo.task_account_scope(a) != ctx.repo.task_account_scope(b)
    marker = root_a / 'monitor/workbench.json'
    own = json.loads(marker.read_text(encoding='utf-8'))
    marker.write_bytes((root_b / 'monitor/workbench.json').read_bytes())
    with pytest.raises(ApiError, match='归属凭证不一致'):
        ctx.routing.audit(a, root_a, starting=True)
    _json(marker, own)
    state_path = root_a / 'monitor/state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['workspace'] = str(root_b / 'workspace')
    _json(state_path, state)
    with pytest.raises(ApiError, match='工作目录'):
        ctx.routing.audit(a, root_a, starting=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(('role', 'username'), [('primary', 'main'), ('standby', 'reserve')])
async def test_desktop_launch_names_bound_solo2_user(
    accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch, role: str, username: str,
) -> None:
    ctx, _ = accounts
    root, task_id = _claim(ctx, role, 'desktop-task')
    pilot = ctx.autopilot
    (pilot.prompt_templates.root / 'chatgpt.md').write_text('自定义桌面提示词\n{notes}', encoding='utf-8')
    app_path = root / 'ChatGPT.app'
    app_path.mkdir()
    monkeypatch.setattr(pilot.desktop, 'app_path', app_path)
    monkeypatch.setattr(pilot.desktop, '_open_link', Mock(return_value=('test', '')))
    monkeypatch.setattr(pilot.desktop, '_wait_for_new_thread_ids', lambda *_: [])
    monkeypatch.setattr(pilot, '_ensure_desktop_follower', lambda _: None)

    job = Job(task_id, root.name)
    await pilot._launch_chatgpt(job, root, [])

    assert job.desktop_running and Path(job.desktop_workdir) == root
    prompt = Path(job.desktop_prompt_file).read_text(encoding='utf-8')
    assert '自定义桌面提示词' in prompt
    opening = urllib.parse.parse_qs(urllib.parse.urlsplit(job.desktop_deep_link).query)['prompt'][0]
    for text in (prompt, opening):
        assert f'本任务绑定的 SOLO2 用户名为 `{username}`' in text
        assert 'AUTOPILOT: BLOCKED SOLO2 用户名与任务绑定不符' in text
        assert f"`{'reserve' if role == 'primary' else 'main'}`" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(('role', 'username', 'stage', 'repair'), [
    ('primary', 'main', 'authoring', False),
    ('standby', 'reserve', 'delivering', False),
    ('standby', 'reserve', 'delivering', True),
])
async def test_cli_launch_names_bound_solo2_user(
    accounts: tuple[AppContext, Any], role: str, username: str, stage: str, repair: bool,
) -> None:
    ctx, _ = accounts
    root, task_id = _claim(ctx, role, 'cli-task')
    pilot = ctx.autopilot
    (pilot.prompt_templates.root / f'{stage}.md').write_text('自定义后台提示词\n{notes}', encoding='utf-8')
    pilot.agent_command = [sys.executable, '-c', (
        'import json, sys\n'
        'print(sys.argv[-1])\n'
        'print(json.dumps({"item": {"type": "agent_message", "text": "AUTOPILOT: OK"}}))'
    )]
    job = Job(task_id, root.name, repair_version='fix-1' if repair else '')
    await pilot._launch(job, stage, root, [])
    run = pilot.agents[task_id]
    await run.waiter

    prompt = run.log.read_text(encoding='utf-8')
    assert f'本任务绑定的 SOLO2 用户名为 `{username}`' in prompt
    assert 'AUTOPILOT: BLOCKED SOLO2 用户名与任务绑定不符' in prompt
    assert ('准备返修' if repair else '自定义后台提示词') in prompt
    assert f"`{'reserve' if role == 'primary' else 'main'}`" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('binding_changed', [False, True])
async def test_launch_refuses_unverified_solo2_account(
    accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch, binding_changed: bool,
) -> None:
    ctx, _ = accounts
    if binding_changed:
        root, task_id = _claim(ctx, 'standby', 'changed-account')
        ctx.settings.solo2.username = 'different-primary'
    else:
        root = ctx.settings.tasks_dir / 'unbound-task'
        root.mkdir(parents=True)
        task_id = ctx.repo.task_id_for_root(root)
    pilot = ctx.autopilot
    desktop_launch = Mock()
    cli_launch = AsyncMock()
    monkeypatch.setattr(pilot.desktop, 'launch', desktop_launch)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', cli_launch)
    job = Job(task_id, root.name)
    expected = 'TASK_OWNER_CHANGED' if binding_changed else 'TASK_SOURCE_REVIEW'

    with pytest.raises(ApiError) as desktop_error:
        await pilot._launch_chatgpt(job, root, [])
    with pytest.raises(ApiError) as cli_error:
        await pilot._launch(job, 'authoring', root, [])

    assert desktop_error.value.code == cli_error.value.code == expected
    desktop_launch.assert_not_called()
    cli_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_solo2_counts_first_submissions_and_only_today_discarded(accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, clock = accounts
    client = ctx.solo2
    monkeypatch.setattr(client, 'login_session', AsyncMock(return_value={
        'ok': True, 'server': 'https://test.invalid', 'account': 'main', 'cookie': 'fixture', 'csrf': 'fixture',
    }))

    def platform(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v1/auth/me':
            return httpx.Response(200, json={'user': {'id': 10, 'username': 'main'}})
        if request.url.path == '/api/v1/submissions/8':
            return httpx.Response(200, json={'timeline': [{'action': 'SUBMIT', 'created_at': '2026-11-17T11:00:00Z'}]})
        if request.url.path == '/api/v1/submissions/9':
            return httpx.Response(200, json={'timeline': [{'action': 'SUBMIT', 'created_at': '2026-11-17T16:00:00Z'}]})
        if request.url.path == '/api/v1/submissions/12':
            return httpx.Response(200, json={
                'id': 12, 'user_id': 10, 'created_at': '2026-11-18T03:00:00Z', 'status': 'SUBMITTED',
            })
        if request.url.path == '/api/v1/submissions/13':
            return httpx.Response(200, json={
                'id': 13, 'user_id': 10, 'created_at': '2026-11-16T03:00:00Z', 'status': 'QC_PASSED',
            })
        if request.url.path == '/api/v1/submissions/14':
            return httpx.Response(404)
        assert request.url.path == '/api/v1/submissions'
        assert request.url.params['user_id'] == '10' and request.url.params['date_from'] == request.url.params['date_to'] == '2026-11-18'
        return httpx.Response(200, json={'items': [
            {'id': 7, 'user_id': 10, 'submitted_at': '2026-11-18T04:00:00Z', 'current_version': 1, 'status': 'SUBMITTED'},
            {'id': 8, 'user_id': 10, 'submitted_at': '2026-11-18T05:00:00Z', 'current_version': 3, 'status': 'DISCARDED'},
            {'id': 9, 'user_id': 10, 'submitted_at': '2026-11-18T06:00:00Z', 'current_version': 2, 'status': 'QC_PASSED'},
            {'id': 10, 'user_id': 10, 'submitted_at': '2026-11-18T07:00:00Z', 'current_version': 1, 'status': 'DISCARDED'},
            {'id': 11, 'user_id': 10, 'submitted_at': '2026-11-18T08:00:00Z', 'current_version': 1, 'status': 'QC_DISCARDED'},
        ], 'meta': {'total_pages': 1}})

    client.transport = httpx.MockTransport(platform)
    submissions = await client.first_submissions(clock.now.date(), local_ids={'8', '12', '13', '14'})
    assert submissions.ids == {'7', '9', '10', '11', '12'}
    assert submissions.discarded_ids == {'10', '11'}


@pytest.mark.asyncio
async def test_routed_standby_intake_uses_only_its_verified_archive(accounts: tuple[AppContext, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, clock = accounts
    _stats(ctx, 'primary', clock.now, 70)
    _stats(ctx, 'standby', clock.now, 39)
    primary, standby = (ctx.routing.lane(role) for role in ('primary', 'standby'))
    assert primary and standby
    main_claim = AsyncMock()
    monkeypatch.setattr(primary.source, 'claim', main_claim)
    claim = AsyncMock(return_value={'name': 'standby-only', 'claim_id': 'secondary-claim'})
    monkeypatch.setattr(standby.source, 'claim', claim)

    async def download(project: str, engineering: str, path: Path) -> str:
        assert (project, engineering) == ('pool-b', 'e-b')
        with zipfile.ZipFile(path, 'w') as zipped:
            zipped.writestr('app.py', 'standby_only = True\n')
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(standby.source, 'download_zip', download)
    candidate = AsyncMock(return_value=[{'id': 'e-b', 'name': 'standby-only'}])
    monkeypatch.setattr(ctx.intake, '_candidate_pool', candidate)
    root = (ctx.settings.tasks_dir / 'standby-only').resolve()

    async def skill(command: list[str], **kwargs: Any) -> SkillResult:
        if command[0] != 'init':
            return SkillResult(code=0, duration=0)
        claim_key = command[command.index('--account-claim') + 1]
        row = ctx.routing.row(claim_key=claim_key)
        source = Path(command[command.index('--source') + 1])
        assert row['owner'] == 'standby' and str(source) == row['meta']['initSourcePath']
        assert command[command.index('--domain') + 1] == '安全'
        assert (source / 'app.py').read_text(encoding='utf-8') == 'standby_only = True\n'
        # 写出技能初始化的最小状态，让工作台按常规入口完成对账。
        workspace = root / 'workspace'
        workspace.mkdir(parents=True)
        (workspace / 'app.py').write_bytes((source / 'app.py').read_bytes())
        _json(root / 'monitor/state.json', {'schema': 'solo-2611/1', 'taskRoot': str(root), 'taskName': root.name, 'workspace': str(workspace),
              'status': 'prepared', 'initialSha': 'fixture-sha', 'accountRoute': standby.route, 'accountClaim': claim_key,
              'fields': {'所属领域': '安全'}})
        return SkillResult(code=0, duration=0, data={'taskRoot': str(root), 'initialSha': 'fixture-sha'})

    monkeypatch.setattr(ctx.gateway, 'call', skill)
    monkeypatch.setattr(ctx.autopilot, 'enroll', AsyncMock())
    plan = {'project_id': 'pool-a', 'mode': 'continuous', 'status': 'running', 'task_ids': [], 'claimed_engineering_ids': []}
    ctx.intake.plan = plan
    await ctx.intake._tick_routed(plan, {'in_hand': 0, 'staged': 0, 'completed': 0})
    main_claim.assert_not_awaited()
    claim.assert_awaited_once_with('pool-b', 'e-b')
    assert candidate.await_args.kwargs['lane'].route == standby.route
    task_id, = plan['task_ids']
    assert ctx.repo.get_task(task_id)['project_id'] == 'pool-b'
    assert ctx.routing.row(task_id=task_id)['meta']['accountRoute'] == standby.route
