from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from solo_auto.gateway.skill import SkillGateway

SCRIPT = r"""
import json
import sys
from pathlib import Path
cmd = sys.argv[1]
if cmd == "version":
    marker = Path(__file__).parent / "version-calls.txt"
    marker.write_text((marker.read_text(encoding="utf-8") if marker.exists() else "") + "x\n", encoding="utf-8")
    print("9.9.9")
elif cmd == "config":
    if sys.argv[2] == "show":
        marker = Path(__file__).parent / "config-calls.txt"
        marker.write_text((marker.read_text(encoding="utf-8") if marker.exists() else "") + "x\n", encoding="utf-8")
    print(json.dumps({"claude": {"maxContainers": 4}}))
elif cmd == "prompt":
    print(json.dumps({"ok": False, "errors": ["题面超限"], "warnings": []}))
    raise SystemExit(2)
elif cmd == "init":
    print("stdout-line")
    print("stderr-line", file=sys.stderr)
    root = Path(sys.argv[sys.argv.index("--task-root") + 1])
    attempt = root / "monitor" / "runs" / "attempt-01"
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "stdout.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    print(json.dumps({"ok": True}))
elif cmd == "repo":
    print("❌ 403 forbidden", file=sys.stderr)
    raise SystemExit(1)
"""


@pytest.fixture
def gateway(tmp_path: Path) -> SkillGateway:
    root = tmp_path / "skill"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "solo2611.py").write_text(SCRIPT, encoding="utf-8")
    return SkillGateway(root, tmp_path / "logs")


@pytest.mark.asyncio
async def test_gateway_parses_zero_and_two(gateway: SkillGateway) -> None:
    assert await gateway.version() == "9.9.9"
    result = await gateway.call(["prompt"], timeout=10)
    assert result.code == 2
    assert result.data == {"ok": False, "errors": ["题面超限"], "warnings": []}


@pytest.mark.asyncio
async def test_gateway_caches_and_single_flights_metadata(gateway: SkillGateway) -> None:
    results = await asyncio.gather(
        *[gateway.version() for _ in range(8)],
        *[gateway.config() for _ in range(8)],
    )
    versions, configs = results[:8], results[8:]
    assert set(versions) == {"9.9.9"}
    assert all(item == {"claude": {"maxContainers": 4}} for item in configs)
    scripts = gateway.script.parent
    assert (scripts / "version-calls.txt").read_text(encoding="utf-8").count("x") == 1
    assert (scripts / "config-calls.txt").read_text(encoding="utf-8").count("x") == 1

    first = await gateway.config()
    first["claude"]["maxContainers"] = 99
    assert (await gateway.config())["claude"]["maxContainers"] == 4

    result = await gateway.call(["config", "set", "claude.maxContainers=4"], timeout=10)
    assert result.code == 0
    await gateway.config()
    assert (scripts / "config-calls.txt").read_text(encoding="utf-8").count("x") == 2


@pytest.mark.asyncio
async def test_gateway_preserves_business_stderr(gateway: SkillGateway) -> None:
    result = await gateway.call(["repo"], timeout=10)
    assert result.code == 1
    assert "403" in result.err
    assert result.data is None


@pytest.mark.asyncio
async def test_gateway_whitelist(gateway: SkillGateway) -> None:
    with pytest.raises(ValueError):
        await gateway.call(["shell"], timeout=10)


def test_clean_env_drops_unrelated_secrets(gateway: SkillGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")
    env = gateway.clean_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "HOME" in env


@pytest.mark.asyncio
async def test_stream_reads_stdout_stderr_and_attempt_file(gateway: SkillGateway, tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    state = {
        "attempts": [
            {
                "index": 1,
                "dir": str(task_root / "monitor" / "runs" / "attempt-01"),
            }
        ]
    }
    state_path = task_root / "monitor" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = await gateway.stream(
        ["init", "--task-root", str(task_root)],
        task_root=task_root,
        timeout=10,
    )
    kinds = {event.kind for event in result.lines}
    assert result.result.code == 0
    assert {"stdout", "stderr", "container"} <= kinds
