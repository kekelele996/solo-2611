from pathlib import Path
from secrets import token_hex

import httpx
import pytest

from solo_auto.app import create_app


@pytest.mark.asyncio
async def test_reveal_only_current_password_after_unlock(tmp_path: Path) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    team_key = token_hex(24)
    github_token = token_hex(24)
    ctx.secrets.set('model.key', team_key)
    ctx.secrets.set('github.token', github_token)
    ctx.secrets.set('solo2.password', 'fixture-platform')
    root = '/api/system/accounts'
    path = root + '/profiles/default/password/'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        assert (await client.post(path + 'gateway')).status_code == 403
        assert (await client.post(path + 'solo2')).status_code == 403
        assert (await client.post(path + 'github')).status_code == 403
        await client.put(root + '/view/code', json={'new': 'fixture-code'})
        assert (await client.post(path + 'solo2')).status_code == 403
        session = await client.post(root + '/view/unlock', json={'code': 'fixture-code', 'purpose': 'profiles'})
        token = session.json()['token']
        headers = {'X-Account-View': token}
        catalog = await client.get(root + '/profiles', headers=headers)
        assert any(row['password_target'] == 'gateway' and row['credential_saved'] for row in catalog.json()['connections'])
        assert team_key not in catalog.text and team_key not in session.text
        assert github_token not in catalog.text and github_token not in session.text
        gateway = await client.post(path + 'gateway', headers=headers)
        assert gateway.json() == {'password': team_key} and gateway.headers['cache-control'] == 'no-store'
        assert 'fixture-platform' not in session.text
        assert 'fixture-platform' not in (await client.get(root + '/profiles', headers=headers)).text
        assert (await client.post(path + 'source', headers=headers)).status_code == 404
        ctx.secrets.set('projecthub.password', 'fixture-source')
        for target, expected in [('solo2', 'fixture-platform'), ('source', 'fixture-source')]:
            result = await client.post(path + target, headers=headers)
            assert result.json() == {'password': expected}
            assert result.headers['cache-control'] == 'no-store'
        assert (await client.post(path + 'github.token', headers=headers)).status_code == 400
        assert (await client.post(path + 'github', headers=headers)).status_code == 403
        settings_session = await client.post(root + '/view/unlock', json={'code': 'fixture-code', 'purpose': 'settings'})
        editor = {'X-Account-View': settings_session.json()['token']}
        assert github_token not in (await client.get('/api/system/config', headers=editor)).text
        github = await client.post(path + 'github', headers=editor)
        assert github.json() == {'password': github_token} and github.headers['cache-control'] == 'no-store'
        assert (await client.post(root + '/profiles/other/password/github', headers=editor)).status_code == 409
        assert (await client.post(path + 'github', headers={**editor, 'Origin': 'https://example.test'})).status_code == 403
        await client.post(root + '/view/lock', headers=editor)
        assert (await client.post(path + 'github', headers=editor)).status_code == 403
        assert (await client.post(path + 'solo2', headers={**headers, 'Origin': 'https://example.test'})).status_code == 403
        assert (await client.post(root + '/profiles/other/password/solo2', headers=headers)).status_code == 409
        ctx.account_access.sessions[token] = (0, 'profiles')
        assert (await client.post(path + 'solo2', headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_view_code_protects_both_pages_and_survives_restart(tmp_path: Path) -> None:
    app = create_app(tmp_path, auto_start=False)
    ctx = app.state.context
    ctx.settings.solo2.username = 'demo-reader'
    ctx.settings.skill.base_url = 'https://test:secret@gateway.example/path?key=secret'
    ctx.secrets.set('github.token', 'fixture-token')
    root = '/api/system/accounts/view'
    config = '/api/system/config'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        assert (await client.get(root + '/status')).json() == {'configured': False}
        assert (await client.post(root + '/unlock', json={'code': 'abcdef'})).status_code == 409
        change = {'new': 'test-code'}
        assert (await client.put(root + '/code', json=change, headers={'Origin': 'https://elsewhere.test'})).status_code == 403
        assert (await client.put(root + '/code', json=change)).status_code == 204
        assert b'test-code' not in ctx.account_access.path.read_bytes()
        assert (await client.get(root + '/status')).json() == {'configured': True}
        assert (await client.put(root + '/code', json={'new': 'another'})).status_code == 403
        assert (await client.post(root + '/unlock', json={'code': 'wrong!'})).status_code == 403
        locked = await client.get(config)
        assert locked.json()['accounts_locked'] and 'demo-reader' not in locked.text
        assert 'gateway.example' not in locked.text and locked.headers['cache-control'] == 'no-store'
        assert (await client.patch(config, json={'solo2': {'username': 'changed'}})).status_code == 403
        assert (await client.post('/api/system/accounts/export', json={'password': 'backup'})).status_code == 403
        skill = {'X-Solo-Skill': 'solo-2611'}
        assert (await client.get(config, headers=skill)).json()['solo2']['username'] == 'demo-reader'
        assert 'demo-reader' not in (await client.get(config, headers={**skill, 'sec-fetch-mode': 'cors'})).text
        view = await client.post(root + '/unlock', json={'code': 'test-code'})
        assert view.headers['cache-control'] == 'no-store' and 'fixture-token' not in view.text
        assert view.json()['token'] is None and 'secret' not in view.text
        rows = view.json()['connections']
        assert rows[0]['account'] == 'demo-reader' and rows[1]['credential_saved']
        assert rows[3]['address'] == 'https://gateway.example/path'
        async def settings_headers() -> dict[str, str]:
            result = await client.post(root + '/unlock', json={'code': 'test-code', 'purpose': 'settings'})
            return {'X-Account-View': result.json()['token']}
        header = await settings_headers()
        assert not (await client.get(config, headers=header)).json()['accounts_locked']
        assert (await client.post(root + '/lock', headers=header)).status_code == 204
        assert (await client.get(config, headers=header)).json()['accounts_locked']
        header = await settings_headers()
        assert (await client.put(root + '/code', json={'current': 'test-code', 'new': 'rotated-code'})).status_code == 204
        assert (await client.get(config, headers=header)).json()['accounts_locked']
    restarted = create_app(tmp_path, auto_start=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as client:
        assert (await client.post(root + '/unlock', json={'code': 'rotated-code'})).status_code == 200
        for _ in range(5):
            assert (await client.post(root + '/unlock', json={'code': 'test-code'})).status_code == 403
        assert (await client.post(root + '/unlock', json={'code': 'test-code'})).status_code == 429
