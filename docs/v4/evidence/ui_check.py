"""Headless visual smoke check for the isolated v4 workbench."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18099"
TASK_ID = sys.argv[1]
OUT = Path(__file__).resolve().parent


def main() -> int:
    result: dict[str, object] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{BASE}/tasks/{TASK_ID}?tab=overview", wait_until="domcontentloaded")
        page.get_by_text("第 1 轮 · 已继续 0/2", exact=True).first.wait_for()
        page.get_by_text("已接受", exact=True).first.wait_for()
        page.screenshot(path=str(OUT / "ui-task-session.png"), full_page=True)
        result["taskSessionPanel"] = True

        page.goto(f"{BASE}/dispatch", wait_until="domcontentloaded")
        page.get_by_text("等待继续", exact=True).first.wait_for()
        page.get_by_text("会话质量", exact=True).wait_for()
        page.screenshot(path=str(OUT / "ui-dispatch-session.png"), full_page=True)
        result["dispatchSessionCapacity"] = True

        page.goto(f"{BASE}/system", wait_until="domcontentloaded")
        page.get_by_text("运行模式", exact=True).wait_for()
        page.get_by_text("继续上限", exact=True).wait_for()
        page.get_by_text("等待处理时限（分钟）", exact=True).wait_for()
        page.get_by_text("无进展时限（秒）", exact=True).wait_for()
        result["legacyAttemptInputCount"] = page.get_by_text("尝试次数", exact=True).count()
        if result["legacyAttemptInputCount"] != 0:
            raise AssertionError("旧尝试次数输入仍在设置页")
        page.screenshot(path=str(OUT / "ui-system-session-settings.png"), full_page=True)
        result["systemSessionSettings"] = True
        browser.close()
    (OUT / "ui-check.json").write_text(
        json.dumps({"ok": True, "base": BASE, "taskId": TASK_ID, **result}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
