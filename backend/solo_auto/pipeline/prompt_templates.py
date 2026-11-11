"""Editable autopilot prompt templates with gate-safety checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.settings import Paths

STAGES = ("authoring", "delivering")
BANNED_LINES = (
    "跳过门禁",
    "绕过门禁",
    "忽略门禁",
    "强制通过",
    "allow-incomplete",
    "ackedBy=user",
    "ackedBy: user",
    'ackedBy": "user',
)
NEGATIONS = ("不得", "不能", "不要", "禁止", "绝不", "不可", "不使用")

DEFAULT_TEMPLATES = {
    "authoring": """你在无人值守模式下工作：全程不要提问，不会有人回复你，遇到选择自己按技能规则决定。
使用 solo-2611 技能。先完整阅读 {skill_root}/SKILL.md，以及它在对应环节要求阅读的 references。
任务目录 ROOT={root}
脚本 S={script}

本次只做「出题」阶段：
1. 读 ROOT/workspace 里的真实源码，按技能的出题规则设计一道「困难」或「地狱」题。
2. 执行 python3 $S prompt --task-root ROOT --init，填好题面草稿和两份审核 JSON，再执行 prompt 安装，直到门禁通过。
3. 执行 python3 $S repo --task-root ROOT，把初始快照推到远端。
4. 执行 python3 $S status --task-root ROOT，确认前三项通过。
边界：不执行 run、interactive、collect，单轮会话由平台排队执行。门禁不过就改题面重来。

通用边界：不修改技能目录、校验器和平台文件；不使用任何强制通过、允许缺项之类绕过门禁的参数；
不伪造任何证据文件。门禁确实过不去时如实报告失败。
全部结束后，最后一行只输出下面两种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
{notes}""",
    "delivering": """你在无人值守模式下工作：全程不要提问，不会有人回复你，遇到选择自己按技能规则决定。
使用 solo-2611 技能。先完整阅读 {skill_root}/SKILL.md，以及它在对应环节要求阅读的 references。
任务目录 ROOT={root}
脚本 S={script}

单轮会话已经结束。本次只做「复核、录屏、Feedback」阶段：
1. 读原始轨迹、轨迹时间线和初始快照到产物快照的 diff，并在本机真实运行产物做复核（技能工作流第 6 步）。
2. 录屏：record plan 生成计划，填写后 record check-plan，再用 python3 $S record auto --task-root ROOT 一次录完。
   Web 题使用计划里的 web 模式与独立 Chrome 流程。产物跑不起来也照录，画面保留真实报错。
3. 执行 feedback --init，按 references/feedback-guide.md 起草 Feedback（文件位置以技能说明为准），
   再执行 feedback --check，改到没有错误为止。
4. 执行 python3 $S status --task-root ROOT，确认第 9 项通过、Feedback 机器检查通过。
边界：不执行 run、reset、export、cleanup。复核时产生的临时文件用完清掉，不提交、不推送。
这次交付没有人工复核，你写下的分数和事实就是最终内容。每个结论都要能回查到轨迹或你亲手跑出的结果，拿不准的不写。

通用边界：不修改技能目录、校验器和平台文件；不使用任何强制通过、允许缺项之类绕过门禁的参数；
不伪造任何证据文件。门禁确实过不去时如实报告失败。
全部结束后，最后一行只输出下面两种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
{notes}""",
}


class PromptTemplates:
    def __init__(self, paths: Paths) -> None:
        self.root = paths.prompts
        self.defaults = self.root / ".defaults"
        self.root.mkdir(parents=True, exist_ok=True)
        self.defaults.mkdir(parents=True, exist_ok=True)
        for stage, text in DEFAULT_TEMPLATES.items():
            default = self.defaults / f"{stage}.md"
            if not default.is_file():
                default.write_text(text, encoding="utf-8")
            active = self.root / f"{stage}.md"
            if not active.is_file():
                active.write_text(text, encoding="utf-8")

    def validate(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for stage in STAGES:
            path = self.root / f"{stage}.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            errors = self._errors(text)
            items.append(
                {
                    "stage": stage,
                    "path": str(path),
                    "ok": not errors and bool(text.strip()),
                    "errors": errors,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
        return items

    def render(
        self,
        stage: str,
        *,
        root: Path,
        skill_root: Path,
        missing: list[str],
        previous: str = "",
    ) -> str:
        if stage not in STAGES:
            raise ApiError(400, "PROMPT_STAGE_INVALID", f"未知 Agent 阶段：{stage}")
        path = self.root / f"{stage}.md"
        try:
            template = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ApiError(409, "PROMPT_TEMPLATE_MISSING", f"提示词模板不存在：{path}") from exc
        errors = self._errors(template)
        if errors:
            raise ApiError(
                409,
                "PROMPT_TEMPLATE_UNSAFE",
                "提示词模板包含绕过门禁的指示，已拒绝启动",
                detail={"path": str(path), "errors": errors},
            )
        notes: list[str] = []
        if missing:
            notes.append("平台当前看到的缺项：")
            notes.extend(f"- {item}" for item in missing[:6])
        if previous and "未完成" in previous:
            notes.append(f"上一次尝试的结果：{previous}")
        try:
            return template.format(
                root=root,
                skill_root=skill_root,
                script=skill_root / "scripts" / "solo2611.py",
                notes="\n".join(notes),
            )
        except (KeyError, ValueError) as exc:
            raise ApiError(
                409,
                "PROMPT_TEMPLATE_INVALID",
                f"提示词模板占位符无效：{path}",
                detail={"error": str(exc)},
            ) from exc

    def listing(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for stage in STAGES:
            path = self.root / f"{stage}.md"
            try:
                text = path.read_text(encoding="utf-8")
                modified = path.stat().st_mtime
            except OSError:
                text, modified = "", 0
            output.append(
                {
                    "stage": stage,
                    "name": f"{stage}.md",
                    "path": str(path),
                    "modified_at": _timestamp(modified),
                    "content": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "errors": self._errors(text),
                }
            )
        return output

    def reset(self, stage: str) -> dict[str, Any]:
        if stage not in STAGES:
            raise ApiError(400, "PROMPT_STAGE_INVALID", f"未知 Agent 阶段：{stage}")
        text = (self.defaults / f"{stage}.md").read_text(encoding="utf-8")
        (self.root / f"{stage}.md").write_text(text, encoding="utf-8")
        return next(item for item in self.listing() if item["stage"] == stage)

    @staticmethod
    def _errors(text: str) -> list[str]:
        errors: list[str] = []
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            lowered = line.lower()
            matched = [term for term in BANNED_LINES if term.lower() in lowered]
            if matched and not any(word in line for word in NEGATIONS):
                errors.append(f"第 {number} 行包含危险指示：{', '.join(matched)}")
        return errors


def _timestamp(value: float) -> str:
    if not value:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(value).astimezone().replace(microsecond=0).isoformat()
