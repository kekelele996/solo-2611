"""Isolated end-to-end check from a task marker through the solo-2611 CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
SKILL = Path.home() / ".codex" / "skills" / "solo-2611" / "scripts" / "solo2611.py"
APP_PORT = 18119
BASE = f"http://127.0.0.1:{APP_PORT}"
OUT = Path(__file__).resolve().parent


def request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def wait_ready(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise RuntimeError(f"工作台提前退出：{stderr}")
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("工作台启动超时")


def run_history(task_root: Path, env: dict[str, str], text: str = "") -> dict[str, Any]:
    argv = [sys.executable, str(SKILL), "prompt-history", "--task-root", str(task_root)]
    if text:
        argv.extend(["--text", text])
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"prompt-history 失败：{result.stderr}\n{result.stdout}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("prompt-history 未返回 JSON 对象")
    return data


def main() -> int:
    result: dict[str, Any] = {
        "ok": False,
        "startedAt": datetime.now(UTC).isoformat(),
        "skill": str(SKILL),
    }
    with tempfile.TemporaryDirectory(prefix="solo-auto-prompt-dedupe-") as temp:
        temp_path = Path(temp)
        home = temp_path / "home"
        env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "SOLO_AUTO_HOME": str(home),
            "SOLO_AUTO_TASKS_DIR": str(temp_path / "tasks"),
            "SOLO_AUTO_DISABLE_KEYCHAIN": "1",
        }
        app_code = (
            "import os; import uvicorn; from pathlib import Path; "
            "from solo_auto.app import create_app; "
            "uvicorn.run(create_app(Path(os.environ['SOLO_AUTO_HOME']), auto_start=False), "
            f"host='127.0.0.1', port={APP_PORT}, log_level='warning')"
        )
        process = subprocess.Popen(
            [str(BACKEND / ".venv" / "bin" / "python"), "-c", app_code],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            wait_ready(process)
            request_json(
                "/api/source/prompts/import",
                {
                    "project_id": "project-a",
                    "source_name": "dedupe-seed.json",
                    "items": [
                        {
                            "text": "把订单按来源时间合并，重复执行不能新增记录。",
                            "engineeringId": "eng-1",
                            "engineeringName": "GB-544",
                            "taskName": "订单对账",
                        },
                        {
                            "text": "对账失败后按错误码拆分重试队列并保留审计记录。",
                            "engineeringId": "eng-1",
                            "engineeringName": "GB-544",
                            "taskName": "错误重试",
                        },
                    ],
                },
            )
            task_root = temp_path / "current-task"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "workbench.json").write_text(
                json.dumps(
                    {
                        "schema": "solo-auto/workbench-task/1",
                        "taskId": "current-task",
                        "taskRoot": str(task_root),
                        "projectId": "project-a",
                        "projectName": "项目 A",
                        "engineeringId": "eng-1",
                        "engineeringName": "GB-544",
                        "apiBase": BASE,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            skill_env = {
                **env,
                "CODEX_HOME": str(temp_path / "codex"),
                "SOLO2611_CONFIG": str(temp_path / "codex" / "solo-2611" / "config.json"),
            }
            history = run_history(task_root, skill_env)
            if history["count"] != 2 or history["historyMeta"]["engineeringName"] != "GB-544":
                raise AssertionError(f"历史查询不完整：{history}")
            result["history"] = {
                "count": history["count"],
                "engineeringName": history["historyMeta"]["engineeringName"],
                "source": history["historyMeta"]["source"],
            }

            duplicate = run_history(
                task_root,
                skill_env,
                "把订单按来源时间合并，重复执行时不能新增记录。",
            )
            if not duplicate["historyMeta"].get("blocking"):
                raise AssertionError(f"重复题面没有被阻断：{duplicate['historyMeta']}")
            result["duplicate"] = duplicate["historyMeta"]["status"]

            clear = run_history(
                task_root,
                skill_env,
                "监控库存周转天数，对异常门店生成可追踪的补货建议。",
            )
            if clear["historyMeta"]["status"] != "clear":
                raise AssertionError(f"新题面被误判：{clear['historyMeta']}")
            result["clear"] = clear["historyMeta"]["status"]
            result["ok"] = True
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    result["finishedAt"] = datetime.now(UTC).isoformat()
    (OUT / "prompt-dedupe-check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
