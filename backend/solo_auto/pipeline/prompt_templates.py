"""Editable autopilot prompt templates with gate-safety checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from solo_auto.errors import ApiError
from solo_auto.settings import Paths

STAGES = ("authoring", "delivering", "chatgpt")
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
全部结束后，最后一行只输出下面三种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
AUTOPILOT: BLOCKED <一句话原因>
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
全部结束后，最后一行只输出下面三种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
AUTOPILOT: BLOCKED <一句话原因>
{notes}""",
    "chatgpt": """使用 `$solo-2611`，在当前 ChatGPT 桌面会话中完成下面这道题，直到平台要求的前 10 项交付门禁全部通过。
先完整阅读 {skill_root}/SKILL.md 及对应 references。任务目录 ROOT={root}，脚本 S={script}。

本次由 solo-auto 生产线明确投递到 ChatGPT 桌面端，不是后台 CLI。必须让工作过程和证据真实发生，禁止只回复计划或总结。

执行顺序：
1. 先运行 python3 $S status --task-root ROOT，读取磁盘现状；已完成的步骤不得重复破坏。
2. 题面尚未通过时完成出题、两份审核、prompt 安装与 repo 初始快照，直到前 3 项通过。
   安装 prompt 时按真实工程填写环境可复现等级，Docker Compose 可一键启动的工程使用
   `--repro "已容器化，可一键起环境"`。
3. 执行 python3 $S run --task-root ROOT，真实运行单轮容器并收集原生产物、SessionID、PromptID、轨迹与产物快照。
4. 按技能规则复核产物。随后生成 record-plan，执行 python3 $S record auto --task-root ROOT 完成 720p 录屏和隐私门禁。
5. 执行 feedback --init，依据轨迹和真实产物起草 Feedback，运行 feedback --check，修正到机器检查通过。
6. 再次运行 status，确认第 1 到第 10 项全部通过。随后执行
   python3 $S export --task-root ROOT --no-submit 生成交付表，再执行
   python3 $S submit --task-root ROOT --execute --watch 提交到 SOLO2 平台，并持续轮询到 QC_PASSED。
   若返回 PENDING_FIX，先对照轨迹和真实产物核对质检意见，再返修原记录；评分和 Feedback 只能写
   轨迹里查得到的事实，不得为了通过质检改写结论。repair 返回 local_review_required（退出码 4）
   表示返修意见与已提交意见出入较大：立即停止，不要登记 local-review 结论，不要调整措辞绕过，
   以 `AUTOPILOT: BLOCKED 等待本地核验` 结束。其他终态失败保留证据并停止。

边界：
- 不修改技能目录或平台文件，不使用强制通过、允许缺项或绕过门禁的参数。
- SOLO2 账号、认证会话和平台地址只能从本地工作台读取；用户名使用工作台配置的用户，
  不得硬编码、猜测或复用历史会话。
- 提交流程只写在本会话指令中，绝对不要写入正式题面文件；题面文件只保留业务需求。
- 不伪造轨迹、截图、录屏、Feedback、分数或命令输出。
- 遇到可恢复错误时按技能规则修正后继续；真实失败时保留证据并停止。
- 所有结论必须能回查到轨迹或亲手执行的结果。
最后一行只输出：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
AUTOPILOT: BLOCKED <一句话原因>
{notes}""",
}

LEGACY_DEFAULT_HASHES = {
    "authoring": "81d46cd77e0e9b5e0d1ff90fb87377d5b0eaa5349cd9d75fc9c7a0005cb540cf",
    "delivering": "1efc7489b24da9d2e8a5f70a766234674f124522c869fa062c6623952acb6300",
    "chatgpt": "8c3b6d959cc92466e696644b1307906637dcfe3c42f665d8878c022e8c1c719e",
}


class PromptTemplates:
    def __init__(self, paths: Paths) -> None:
        self.root = paths.prompts
        self.defaults = self.root / ".defaults"
        self.root.mkdir(parents=True, exist_ok=True)
        self.defaults.mkdir(parents=True, exist_ok=True)
        for stage, text in DEFAULT_TEMPLATES.items():
            default = self.defaults / f"{stage}.md"
            if not default.is_file() or default.read_text(encoding="utf-8") != text:
                default.write_text(text, encoding="utf-8")
            active = self.root / f"{stage}.md"
            if not active.is_file() or hashlib.sha256(active.read_bytes()).hexdigest() == LEGACY_DEFAULT_HASHES[stage]:
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
        history: list[str] | None = None,
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
        if history:
            notes.extend(history)
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
