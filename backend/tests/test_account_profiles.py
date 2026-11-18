import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from solo_auto.api.profiles import _standby_key
from solo_auto.app import create_app


@pytest.mark.asyncio
async def test_profile_switch_round_trip_and_skill_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    root = '/api/system/accounts'
    ctx.settings.solo2.username = 'first'
    ctx.secrets.set('solo2.password', 'original-secret')
    ctx.secrets.set_json('projecthub.cookies', {'sid': 'first-session'})
    ctx.save_settings()
    staged = ctx.staging.directory('engineering')
    staged.mkdir(parents=True)
    (staged / 'meta.json').write_text(json.dumps({'engineeringId': 'engineering'}))
    ctx.intake.state_file.write_text(json.dumps({'plan': {'id': 'first-plan', 'status': 'paused'}}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as c:
        assert (await c.get(root + '/profiles')).status_code == 403
        await c.put(root + '/view/code', json={'new': 'fixture-pass'})
        async def unlock(scope: str) -> dict[str, str]:
            r = await c.post(root + '/view/unlock', json={'code': 'fixture-pass', 'purpose': scope})
            return {'X-Account-View': r.json()['token']}
        editor = await unlock('settings')
        switcher = await unlock('profiles')
        draft = {'name': '第二套', 'solo2': {'username': 'second', 'locked_domain': '安全'},
                 'source': {'username': 'other'}, 'model_base_url': 'https://example.test/gateway',
                 'secrets': {'solo2_password': 'second-secret', 'gateway_key': 'separate-key'}}
        assert (await c.post(root + '/profiles', headers=switcher, json=draft)).status_code == 403
        created = await c.post(root + '/profiles', headers=editor, json=draft)
        assert created.status_code == 201 and created.json()['active'] == 'default'
        second = created.json()['items'][1]['id']
        dispatch_url = '/api/dispatch?compact=1&metrics=false'
        initial = await c.get(dispatch_url)
        assert initial.json()['active_profile_name'] == '默认配置'
        quota = initial.json()['submission_quota']
        assert {k: quota[k] for k in ('open', 'date', 'limit')} == {
            'open': True, 'date': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(), 'limit': 70}
        marker = Path(quota['primary']['quota_directory']) / quota['date']
        marker.parent.mkdir(parents=True)
        marker.write_text('{}')
        assert '第二套' not in initial.text and 'profiles' not in initial.json()
        assert (await c.post(root + '/profiles', headers=editor, json=draft)).status_code == 409
        assert ctx.secrets.get('solo2.password') == 'original-secret'
        switch = root + f'/profiles/{second}/activate'
        with monkeypatch.context() as patch:
            patch.setattr(ctx.dispatcher, 'in_hand_count', lambda: 1)
            assert (await c.post(switch, headers=switcher)).status_code == 409
            assert ctx.settings.active_profile == 'default'
        assert (await c.get(dispatch_url)).json()['active_profile_name'] == '默认配置'
        assert (await c.post(switch, headers=switcher)).json()['active'] == second
        switched = (await c.get(dispatch_url)).json()
        assert switched['active_profile_name'] == '第二套'
        assert switched['submission_quota']['open'] is True
        second_marker = Path(switched['submission_quota']['primary']['quota_directory']) / switched['submission_quota']['date']
        second_marker.parent.mkdir(parents=True)
        second_marker.write_text('{}')
        assert (await c.get(dispatch_url)).json()['submission_quota']['open'] is False
        assert ctx.staging.list() == [] and ctx.intake.plan is None
        assert ctx.staging.directory('engineering') != staged
        assert ctx.source is ctx.intake.source is ctx.dispatcher.source is ctx.preflight.source
        assert ctx.secrets.get('solo2.password') == 'second-secret'
        assert not ctx.secrets.has('projecthub.cookies') and ctx.secrets.get('model.key') == 'separate-key'
        assert (await c.patch('/api/system/config', headers={**editor, 'X-Account-Profile': 'default'},
                              json={'secrets': {'github_token': 'stale'}})).status_code == 409
        skill = {'X-Solo-Skill': 'solo-2611'}
        config = (await c.get('/api/system/config', headers=skill)).json()
        assert (config['active_profile'], config['solo2']['username'], config['solo2']['locked_domain']) == (second, 'second', '安全')
        assert (await c.post('/api/system/solo2/session', headers={**skill, 'X-Account-Profile': 'default'})).status_code == 409
        await c.patch('/api/system/config', headers={**editor, 'X-Account-Profile': second}, json={'secrets': {'github_token': 'current-token'}})
        assert (await c.post(root + '/profiles/default/activate', headers=switcher)).json()['active'] == 'default'
        assert ctx.secrets.get('solo2.password') == 'original-secret' and not ctx.secrets.has('github.token')
        assert ctx.staging.list()[0]['engineeringId'] == 'engineering' and ctx.intake.plan['id'] == 'first-plan'
        assert ctx.secrets.get_json('projecthub.cookies') == {'sid': 'first-session'}
        await c.post(switch, headers=switcher)
        restarted = create_app(tmp_path, auto_start=False).state.context
        assert restarted.settings.solo2.username == 'second' and restarted.secrets.get('github.token') == 'current-token'
        assert all(s not in ctx.paths.config.read_text() + created.text for s in ['original-secret', 'second-secret', 'current-token', 'separate-key'])


@pytest.mark.asyncio
async def test_standby_follows_its_primary_and_secrets_never_enter_catalog(tmp_path: Path) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    ctx.settings.solo2.username = 'main-a'
    ctx.save_settings()
    base = '/api/system/accounts/profiles'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as c:
        await c.put('/api/system/accounts/view/code', json={'new': 'fixture-pass'})
        async def unlock(purpose: str) -> dict[str, str]:
            r = await c.post('/api/system/accounts/view/unlock', json={'code': 'fixture-pass', 'purpose': purpose})
            return {'X-Account-View': r.json()['token']}
        edit = await unlock('settings')
        view = await unlock('profiles')
        created = await c.post(base, headers=edit, json={'name': 'B', 'solo2': {'username': 'main-b'}})
        second = created.json()['items'][-1]['id']
        path = base + '/default/standby'
        a = {'primary_username': 'main-a', 'primary_address': ctx.settings.solo2.base_url, 'username': 'alt-a',
             'domain': '安全', 'password': 'fixture-pass-a', 'github_token': 'fixture-token-a'}
        assert (await c.put(path, headers=view, json=a)).status_code == 403
        r = await c.put(path, headers=edit, json=a)
        assert r.status_code == 200 and r.json()['standby']['username'] == 'alt-a' and len(r.json()['items']) == 2
        assert a['password'] not in r.text and a['github_token'] not in r.text
        assert (await c.put(path, headers=edit, json=a)).status_code == 409
        revision = r.json()['standby']['revision']
        assert (await c.put(path, headers=edit, json={**a, 'revision': revision, 'username': 'different',
                                                     'password': '', 'github_token': ''})).status_code == 422
        r = await c.put(path, headers=edit, json={**a, 'revision': revision, 'password': '', 'github_token': ''})
        assert r.status_code == 200
        switched = await c.post(base + f'/{second}/activate', headers=view)
        assert switched.json()['standby'] is None
        assert (await c.put(path, headers=edit, json=a)).status_code == 409
        b = {**a, 'primary_username': 'main-b', 'username': 'alt-b', 'password': 'fixture-pass-b', 'github_token': 'fixture-token-b'}
        r = await c.put(base + f'/{second}/standby', headers=edit, json=b)
        assert r.json()['standby']['username'] == 'alt-b' and 'alt-a' not in r.text
        assert not ctx.secrets.has('github.token') and not ctx.secrets.has('solo2.password')
        back = await c.post(base + '/default/activate', headers=view)
        assert back.json()['standby']['username'] == 'alt-a' and 'alt-b' not in back.text
        await c.patch('/api/system/config', headers=edit, json={'solo2': {'username': 'main-c'}})
        assert (await c.get(base, headers=view)).json()['standby'] is None
        assert (await c.put(path, headers=edit, json=a)).status_code == 409
        await c.patch('/api/system/config', headers=edit, json={'solo2': {'username': 'main-a'}})
    restarted = create_app(tmp_path, auto_start=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as fresh:
        login = await fresh.post('/api/system/accounts/view/unlock', json={'code': 'fixture-pass', 'purpose': 'profiles'})
        r = await fresh.get(base, headers={'X-Account-View': login.json()['token']})
        assert r.json()['standby']['username'] == 'alt-a'
    assert all(secret not in ctx.paths.config.read_text() for secret in (a['password'], a['github_token'], b['password'], b['github_token']))


@pytest.mark.asyncio
async def test_standby_recovers_hex_keychain_record_and_requires_write_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    ctx.settings.solo2.username = 'primary'
    key = _standby_key(ctx.settings.solo2)
    legacy = {'username': 'secondary', 'domain': '数据工程', 'revision': 'old',
              'password': '旧测试密码', 'github_token': 'fixture-gh'}
    ctx.secrets.set(key, json.dumps(legacy, ensure_ascii=False).encode().hex())
    base = '/api/system/accounts/profiles'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as c:
        await c.put('/api/system/accounts/view/code', json={'new': 'fixture-pass'})
        unlock = await c.post('/api/system/accounts/view/unlock', json={'code': 'fixture-pass', 'purpose': 'settings'})
        headers = {'X-Account-View': unlock.json()['token']}
        old = await c.get(base, headers=headers)
        assert old.json()['standby'] == {'username': 'secondary', 'domain': '数据工程', 'revision': 'old'}
        draft = {**old.json()['standby'], 'domain': '安全', 'primary_username': 'primary',
                 'primary_address': ctx.settings.solo2.base_url}
        with monkeypatch.context() as patch:
            patch.setattr(ctx.secrets, 'set_json', lambda *_: None)
            failure = await c.put(base + '/default/standby', headers=headers, json=draft)
            assert failure.status_code == 500 and failure.json()['error']['code'] == 'STANDBY_SAVE_FAILED'
        assert ctx.secrets.get_json(key) == legacy
        saved = await c.put(base + '/default/standby', headers=headers, json=draft)
        assert saved.status_code == 200
        wire = ctx.secrets.get(key)
        assert wire and wire.isascii()
        record = json.loads(wire)
        assert record['domain'] == '安全' and record['revision'] != legacy['revision']
        assert record['password'] == legacy['password'] and record['github_token'] == legacy['github_token']
        assert saved.json()['standby'] == (await c.get(base, headers=headers)).json()['standby']


@pytest.mark.asyncio
async def test_standby_connection_checks_saved_or_entered_credentials_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    ctx.settings.solo2.username = 'primary'
    ctx.secrets.set('solo2.password', 'primary-password')
    ctx.secrets.set('github.token', 'primary-token')
    seen: list[httpx.Request] = []

    def platform(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == 'api.github.com':
            if request.headers['Authorization'] == 'Bearer fixture-secondary-token':
                return httpx.Response(200, json={'login': 'secondary-github'})
            return httpx.Response(401, json={'message': 'bad credentials'})
        login = json.loads(request.content)
        if login['username'] == 'secondary' and login['password'] == '副号测试密码':
            return httpx.Response(200, json={'csrf_token': 'fixture-csrf',
                                             'user': {'username': 'secondary', 'domain': '数据工程'}},
                                  headers={'set-cookie': 'fixture_sid=secondary; Path=/; HttpOnly'})
        return httpx.Response(401)

    create_client = httpx.AsyncClient

    def outbound_client(**kwargs: Any) -> httpx.AsyncClient:
        return create_client(**(kwargs | {'transport': httpx.MockTransport(platform)}))

    base = '/api/system/accounts/profiles/default/standby'
    draft = {'username': 'secondary', 'domain': '数据工程', 'primary_username': 'primary',
             'primary_address': ctx.settings.solo2.base_url, 'password': '副号测试密码',
             'github_token': 'fixture-secondary-token'}
    async with create_client(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as c:
        monkeypatch.setattr(httpx, 'AsyncClient', outbound_client)
        await c.put('/api/system/accounts/view/code', json={'new': 'fixture-pass'})

        async def unlock(purpose: str) -> dict[str, str]:
            r = await c.post('/api/system/accounts/view/unlock', json={'code': 'fixture-pass', 'purpose': purpose})
            return {'X-Account-View': r.json()['token']}

        edit = await unlock('settings')
        view = await unlock('profiles')
        assert (await c.post(base + '/test', headers=view, json=draft)).status_code == 403
        assert (await c.post(base + '/test', headers=edit, json=draft | {'primary_username': 'stale'})).status_code == 409
        assert not seen
        draft_test = await c.post(base + '/test', headers=edit, json=draft | {'github_token': 'bad-token'})
        assert draft_test.json()['solo2']['ok'] is True and draft_test.json()['github']['ok'] is False
        assert ctx.secrets.get_json(_standby_key(ctx.settings.solo2)) is None
        saved = await c.put(base, headers=edit, json=draft)
        assert saved.status_code == 200 and len(seen) == 2
        selected = draft | {'revision': saved.json()['standby']['revision'], 'password': '', 'github_token': ''}
        mismatch_test = await c.post(base + '/test', headers=edit, json=selected | {'domain': '安全'})
        assert mismatch_test.json()['solo2']['ok'] is False and mismatch_test.json()['github']['ok'] is True
        result = await c.post(base + '/test', headers=edit, json=selected)
        assert result.json() == {
            'solo2': {'ok': True, 'message': 'Solo2 登录成功：secondary。账号领域已核验'},
            'github': {'ok': True, 'message': 'GitHub 连接成功：secondary-github'},
        }
        assert all(value not in result.text for value in ('fixture-secondary-token', 'primary-token', 'primary-password'))
        assert ctx.secrets.get_json(_standby_key(ctx.settings.solo2))['revision'] == selected['revision']
        assert ctx.secrets.get('solo2.password') == 'primary-password' and ctx.secrets.get('github.token') == 'primary-token'
        skill = {'X-Solo-Skill': 'solo-2611', 'X-Account-Profile': ctx.settings.active_profile}
        config = await c.get('/api/system/config', headers=skill)
        standby = config.json()['solo2']['standby']
        assert (standby['username'], standby['locked_domain']) == ('secondary', '数据工程')
        assert standby['quota_directory'] != config.json()['solo2']['quota_directory']
        assert all(secret not in config.text for secret in ('副号测试密码', 'fixture-secondary-token', 'primary-token'))
        assert standby['revision'] == selected['revision']
        credential = await c.post('/api/system/skill/credential/github_standby',
                                  headers=skill | {'X-Standby-Revision': standby['revision']})
        assert credential.json() == {'value': 'fixture-secondary-token'}
        stale_credential = await c.post('/api/system/skill/credential/github_standby',
                                        headers=skill | {'X-Standby-Revision': 'stale'})
        assert stale_credential.status_code == 409
        session = await c.post('/api/system/solo2/standby-session', headers=skill,
                               json={'primary_username': 'primary', 'primary_address': draft['primary_address']})
        assert session.status_code == 200
        assert (session.json()['account'], session.json()['standby'], session.json()['locked_domain']) == (
            'secondary', True, '数据工程')
        assert session.json()['standby_revision'] == standby['revision']
        assert all(secret not in session.text for secret in ('副号测试密码', 'fixture-secondary-token'))
        stale = await c.post('/api/system/solo2/standby-session', headers=skill,
                             json={'primary_username': 'other', 'primary_address': draft['primary_address']})
        assert stale.status_code == 409
        dispatch = (await c.get('/api/dispatch?metrics=false')).json()['submission_quota']
        assert dispatch['open'] is True and dispatch['primary']['open'] is True and dispatch['standby']['open'] is True
        day = dispatch['date']
        primary_marker = Path(dispatch['primary']['quota_directory']) / day
        primary_marker.parent.mkdir(parents=True, exist_ok=True)
        primary_marker.write_text('{}')
        dispatch = (await c.get('/api/dispatch?metrics=false')).json()['submission_quota']
        assert dispatch['open'] is True and dispatch['primary']['open'] is False and dispatch['standby']['open'] is True
        standby_marker = Path(dispatch['standby']['quota_directory']) / day
        standby_marker.parent.mkdir(parents=True, exist_ok=True)
        standby_marker.write_text('{}')
        dispatch = (await c.get('/api/dispatch?metrics=false')).json()['submission_quota']
        assert dispatch['open'] is False and dispatch['standby']['open'] is False
