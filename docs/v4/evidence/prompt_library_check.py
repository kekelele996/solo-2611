"""Isolated UI acceptance for the project prompt library."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
EVIDENCE = Path(__file__).resolve().parent
APP_PORT = 18109
SOURCE_PORT = 18108


class SourceHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/v1/auth/login":
            self._json(
                {"csrf": "ui-csrf", "user": {"id": "ui-user", "name": "验收用户"}},
                headers={"Set-Cookie": "session=ui; Path=/; HttpOnly"},
            )
            return
        self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/v1/me/projects"):
            self._json(
                {
                    "items": [
                        {
                            "id": "project-ui",
                            "name": "提示词库验收项目",
                            "status": "active",
                            "engineering_count": 1,
                        }
                    ]
                }
            )
            return
        if "/engineering" in self.path:
            self._json(
                {
                    "items": [
                        {
                            "id": "engineering-ui",
                            "name": "验收工程",
                            "language": "Python",
                            "source_file_count": 12,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 200,
                }
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _json(self, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _request_json(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _wait_ready(base: str, process: subprocess.Popen[bytes], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise RuntimeError(f"工作台提前退出：{stderr}")
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("工作台启动超时")


def main() -> int:
    source = ThreadingHTTPServer(("127.0.0.1", SOURCE_PORT), SourceHandler)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    base = f"http://127.0.0.1:{APP_PORT}"
    result: dict[str, Any] = {
        "ok": False,
        "base": base,
        "source": f"http://127.0.0.1:{SOURCE_PORT}",
        "startedAt": datetime.now(UTC).isoformat(),
    }

    with tempfile.TemporaryDirectory(prefix="solo-auto-prompt-library-ui-") as temp:
        temp_path = Path(temp)
        home = temp_path / "home"
        tasks = temp_path / "tasks"
        env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "SOLO_AUTO_HOME": str(home),
            "SOLO_AUTO_TASKS_DIR": str(tasks),
            "SOLO_AUTO_SOURCE_URL": f"http://127.0.0.1:{SOURCE_PORT}",
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
            _wait_ready(base, process)
            _request_json(
                base,
                "/api/source/login",
                method="POST",
                payload={"username": "ui", "password": "ui"},
            )
            seeded = _request_json(
                base,
                "/api/source/prompts/import",
                method="POST",
                payload={
                    "project_id": "project-ui",
                    "source_name": "seed.json",
                    "items": [
                        {"text": "按来源时间合并订单，重复执行不能新增记录。"},
                        {"text": "为对账结果补上可追溯的批次编号。"},
                        {"text": "按来源时间合并订单，重复执行不能新增记录。"},
                    ],
                },
            )
            if seeded["imported"] != 2 or seeded["skipped"] != 1:
                raise AssertionError(f"接口导入去重异常：{seeded}")

            new_prompt = temp_path / "prompts.jsonl"
            new_prompt.write_text(
                '{"text":"灰度批次失败时要保留原始输入并提示重试。"}\n',
                encoding="utf-8",
            )
            duplicate = temp_path / "duplicate.json"
            duplicate.write_text(
                json.dumps(
                    {"items": [{"text": "按来源时间合并订单，重复执行不能新增记录。"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1100},
                    accept_downloads=True,
                )
                page = context.new_page()
                page.goto(f"{base}/source", wait_until="domcontentloaded")
                page.locator(".prompt-library-title").wait_for()
                page.get_by_text("按来源时间合并订单，重复执行不能新增记录。", exact=True).wait_for()
                result["projectSelected"] = True
                result["seedRendered"] = True

                page.get_by_placeholder("搜索题面、任务名、子方向").fill("批次编号")
                page.get_by_text("为对账结果补上可追溯的批次编号。", exact=True).wait_for()
                if page.get_by_text("按来源时间合并订单，重复执行不能新增记录。", exact=True).count():
                    raise AssertionError("搜索后仍显示不匹配的提示词")
                page.get_by_placeholder("搜索题面、任务名、子方向").fill("")
                result["search"] = True

                file_input = page.locator('input[type="file"]')
                file_input.set_input_files(str(new_prompt))
                page.get_by_text("导入 1 条，跳过重复或无效 0 条", exact=True).wait_for()
                page.get_by_text("灰度批次失败时要保留原始输入并提示重试。", exact=True).wait_for()
                result["uiImport"] = True

                file_input.set_input_files(str(duplicate))
                page.get_by_text("导入 0 条，跳过重复或无效 1 条", exact=True).wait_for()
                result["deduplicate"] = True

                with page.expect_download() as download_info:
                    page.get_by_role("button", name="导出", exact=True).click()
                download = download_info.value
                exported_path = temp_path / "exported.json"
                download.save_as(exported_path)
                exported = json.loads(exported_path.read_text(encoding="utf-8"))
                if exported["schema"] != "solo-auto/prompt-library/1":
                    raise AssertionError("导出 schema 不正确")
                if len(exported["items"]) != 3:
                    raise AssertionError(f"导出条数不正确：{len(exported['items'])}")
                result["exportItems"] = len(exported["items"])
                result["exportFilename"] = download.suggested_filename

                file_input.set_input_files(str(exported_path))
                page.get_by_text("导入 0 条，跳过重复或无效 3 条", exact=True).wait_for()
                result["roundTripDeduplicate"] = True

                page.screenshot(path=str(EVIDENCE / "ui-source-prompt-library.png"), full_page=True)
                context.close()
                browser.close()
            result["ok"] = True
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            source.shutdown()
            source.server_close()

    result["finishedAt"] = datetime.now(UTC).isoformat()
    (EVIDENCE / "prompt-library-check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
