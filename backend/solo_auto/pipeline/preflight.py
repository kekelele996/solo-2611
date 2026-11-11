"""Preflight checks that block unattended delivery before work starts."""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Any

from solo_auto.gateway.skill import SkillGateway
from solo_auto.pipeline.autopilot import Autopilot
from solo_auto.pipeline.prompt_templates import PromptTemplates
from solo_auto.secrets import SecretStore
from solo_auto.settings import Paths, Settings
from solo_auto.sources.project_hub import ProjectHubClient


class Preflight:
    def __init__(
        self,
        *,
        paths: Paths,
        settings: Settings,
        gateway: SkillGateway,
        autopilot: Autopilot,
        templates: PromptTemplates,
        source: ProjectHubClient,
        secrets: SecretStore,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.gateway = gateway
        self.autopilot = autopilot
        self.templates = templates
        self.source = source
        self.secrets = secrets

    async def run(self) -> dict[str, Any]:
        skill_version = await self.gateway.version()
        skill_config = await self.gateway.config()
        docker_ok, source_result = await asyncio.gather(_docker_ok(), self.source.test())
        disk_free = shutil.disk_usage(self.paths.home).free / 1024**3
        raw_claude = skill_config.get("claude")
        raw_github = skill_config.get("github")
        claude: dict[str, Any] = raw_claude if isinstance(raw_claude, dict) else {}
        github: dict[str, Any] = raw_github if isinstance(raw_github, dict) else {}
        template_errors = [
            f"{item['stage']}: {'；'.join(item['errors'])}"
            for item in self.templates.validate()
            if not item["ok"]
        ]
        if self.settings.autopilot.agent_mode == "chatgpt":
            agent_ok = self.autopilot.desktop.available()
            agent_message = (
                f"ChatGPT 桌面深链接可用：{self.autopilot.desktop.helper or 'open'}"
                if agent_ok
                else "ChatGPT.app 或 CodexQueuePush 不可用"
            )
            agent_fix = "安装 ChatGPT 桌面端并授权 CodexQueuePush 辅助功能"
        else:
            agent_ok = bool(shutil.which(self.autopilot.agent_command[0]))
            agent_message = f"Agent：{self.autopilot.agent_command[0]}"
            agent_fix = "安装 Codex CLI，或在自动交付设置中填写 agent_bin"
        items = [
            _item(
                "skill_version",
                _version_tuple(skill_version) >= (0, 2, 3),
                f"技能版本 {skill_version or '未知'}",
                "升级 solo-2611 到 0.2.3 或更高版本",
            ),
            _item(
                "agent",
                agent_ok,
                agent_message,
                agent_fix,
            ),
            _item(
                "prompt_templates",
                not template_errors,
                "提示词模板自检通过" if not template_errors else "；".join(template_errors),
                "移除模板中绕过门禁或冒充人工确认的指示",
            ),
            _item(
                "github_token",
                bool(self.secrets.get("github.token") or github.get("accessTokens")),
                "GitHub Token 已配置" if self.secrets.get("github.token") or github.get("accessTokens") else "GitHub Token 未配置",
                "在技能配置中设置 github.accessTokens",
            ),
            _item(
                "gateway_key",
                bool(claude.get("apiKey") and claude.get("baseUrl")),
                "网关 Key 已配置" if claude.get("apiKey") and claude.get("baseUrl") else "网关 Key 或地址缺失",
                "运行 solo2611 config set claude.baseUrl=<地址> claude.apiKey=<团队 Key>",
            ),
            _item(
                "docker",
                docker_ok,
                "Docker daemon 可用" if docker_ok else "Docker daemon 不可用",
                "启动 Docker Desktop 后重试",
            ),
            _item(
                "disk",
                disk_free >= self.settings.storage.disk_floor_gb,
                f"磁盘剩余 {disk_free:.1f}GB",
                f"清理磁盘，至少保留 {self.settings.storage.disk_floor_gb:g}GB",
            ),
            _item(
                "source",
                bool(source_result.get("ok")),
                str(source_result.get("message") or "题源登录态可用"),
                "到题源页重新登录工程台",
            ),
            _item(
                "screen_recording",
                sys.platform != "darwin" or bool(shutil.which("screencapture")),
                "系统录屏命令可用" if sys.platform == "darwin" else "当前平台不需要 macOS 录屏权限",
                "在系统设置 > 隐私与安全性 > 屏幕录制中授权终端",
            ),
        ]
        return {
            "ok": all(item["ok"] for item in items if item["level"] == "P0"),
            "items": items,
            "checked_at": _now(),
        }


def _item(key: str, ok: bool, message: str, fix: str, level: str = "P0") -> dict[str, Any]:
    return {"key": key, "level": level, "ok": ok, "message": message, "fix": "" if ok else fix}


def _version_tuple(value: str) -> tuple[int, ...]:
    output: list[int] = []
    for part in value.replace("-", ".").split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        output.append(int(digits))
    return tuple(output or [0])


async def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "info",
        "--format",
        "{{.ServerVersion}}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().replace(microsecond=0).isoformat()
