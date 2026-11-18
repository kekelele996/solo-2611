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

# 三种派发方式使用同一套开场规则；占位符保持兼容磁盘上的可编辑模板。
TASK_START = """任务目录 ROOT 为 {root}，脚本 S 为 {script}。
先运行 python3 "$S" status --task-root "$ROOT" --brief，根据未通过项接着做本阶段的工作。
当前会话已有 solo-2611 正文时沿用它；只有尚未加载时才读一次 {skill_root}/SKILL.md。参考资料在做到相应步骤时读取。"""

DEFAULT_TEMPLATES = {
    "authoring": """你在无人值守模式下工作：全程不要提问，不会有人回复你，遇到选择自己按技能规则决定。
使用 solo-2611 技能。
""" + TASK_START + """

本次只做「出题」阶段：
1. 读 ROOT/workspace 里的真实源码，按技能的出题规则设计一道「困难」或「地狱」题。
2. 执行 python3 $S prompt --task-root ROOT --init，填好题面草稿和两份审核 JSON，再执行 prompt 安装，直到门禁通过。
3. 执行 python3 $S repo --task-root ROOT，把初始快照推到远端。
4. 用状态简报确认初始快照、GitHub 初始快照和题面均已通过。
边界：不执行 run、interactive、collect，单轮会话由平台排队执行。门禁不过就改题面重来。

通用边界：不修改技能目录、校验器和平台文件；不使用任何强制通过、允许缺项之类绕过门禁的参数；
不伪造任何证据文件。门禁确实过不去时如实报告失败。
全部结束后，最后一行只输出下面三种之一：
AUTOPILOT: OK
AUTOPILOT: FAILED <一句话原因>
AUTOPILOT: BLOCKED <一句话原因>
{notes}""",
    "delivering": """你在无人值守模式下工作：全程不要提问，不会有人回复你，遇到选择自己按技能规则决定。
使用 solo-2611 技能。
""" + TASK_START + """

单轮会话已经结束。本次只做「复核、录屏、Feedback」阶段：
1. 读原始轨迹、轨迹时间线和初始快照到产物快照的 diff，并在本机真实运行产物做复核（技能工作流第 6 步）。
2. 生成并检查录屏计划。执行 record auto，由计划选择真实终端或网页双窗口录制；网页脚本在每次输入或点击之间停留约三秒，验证最终业务结果。启动失败按终端计划录下真实报错。
3. 执行 feedback --init，按 references/feedback-guide.md 起草 Feedback（文件位置以技能说明为准），
   再执行 feedback --check，改到没有错误为止。
4. 用状态简报确认录屏和 Feedback 均已通过。
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
""" + TASK_START + """

本次由 solo-auto 生产线明确投递到 ChatGPT 桌面端，不是后台 CLI。必须让工作过程和证据真实发生，禁止只回复计划或总结。

执行顺序：
1. 先补齐尚缺的题面、两份审核和 repo 初始快照，已通过的步骤沿用现有材料。
   安装 prompt 时按真实工程填写环境可复现等级，Docker Compose 可一键启动的工程使用
   `--repro "已容器化，可一键起环境"`。
2. 首轮尚未启动才执行 run。已有运行记录时先用 run-status --brief --json 核对；仍在运行就接回原会话，已接受则复用产物、SessionID、PromptID 和轨迹。异常终态按技能规则处理。
3. 受管会话仍在运行时执行一次 python3 "$S" run-status --task-root "$ROOT" --wait-until attention --timeout 11400 --interval 15 --brief --json，按返回状态继续。
4. 按技能规则复核产物，生成并检查录屏计划。执行 record auto。网页计划包含独立窗口的 pageCommand、操作间约三秒停顿和真实结果断言；终端计划展示命令与输出，启动失败保留真实报错。结果画面保留约六秒。
5. 根据真实轨迹和产物编写 Feedback，运行 feedback --check。遇到评分与轨迹冲突，保留核验材料并结束本题。
6. 前十项材料就绪后结束本会话。工作台独立负责导出、提交和查询回执。已实现而模型询问是否提交时正常推进；复核发现题面缺业务条件时按实际结果交付。

边界：
- 不修改技能目录或平台文件，不使用强制通过、允许缺项或绕过门禁的参数。
- SOLO2 账号、认证会话和平台地址只能从本地工作台读取；用户名使用工作台配置的用户，
  不得硬编码、猜测或复用历史会话。
- 当前会话不调用 export、submit、submission-status 或 repair。
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


PRIOR_TEMPLATE_HASHES = {'authoring': 'af726d5ba6f059b44c9465651c74f8e367e87c92ff8d3ed7e5c49ed8e2718514', 'delivering': 'd45e9fced8e44cff6634349e8e51572569cc0a413793a9aa46197f3244910344', 'chatgpt': '88642cf9ba05299e5fe50546dda48714223d89dfc222b26119698ca7650c4954'}


RECORDING_TEMPLATE_HASHES = {'authoring': 'af726d5ba6f059b44c9465651c74f8e367e87c92ff8d3ed7e5c49ed8e2718514', 'delivering': 'b691f3bfe7dd5b10d66a419cd435e9f77d9beca60f7b30f0b779add27d487351', 'chatgpt': '71e957488d926df5e405e0a856b91e710cee50c7aaf291e5cade32d66ed19feb'}

# 上一版随工作台安装的模板。只有内容仍完全匹配时才自动更新。
PRE_BRIEF_HASHES = {
    "authoring": "af726d5ba6f059b44c9465651c74f8e367e87c92ff8d3ed7e5c49ed8e2718514",
    "delivering": "0bae40e767fc6546e73c679de5a44ac036132837a27074f9db370158cd9237e8",
    "chatgpt": "efa5f7d1527e7e6d0d69a1b8a2495502e492fb3c2ff2da7ceaf35a99d27e55e7",
}

# 8e2834d7 出厂的桌面模板仍会直接提交和轮询，需要与当前工作台同步。
HISTORICAL_CHATGPT_TEMPLATE = "a104c6018e7a237ff99b8903bd218e5567abb3f3c59b7adb5c6729d4d65ea24b"


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
            old_defaults = {LEGACY_DEFAULT_HASHES[stage], PRIOR_TEMPLATE_HASHES[stage], RECORDING_TEMPLATE_HASHES[stage], PRE_BRIEF_HASHES[stage]}
            if stage == "chatgpt":
                old_defaults.add(HISTORICAL_CHATGPT_TEMPLATE)
            if not active.is_file() or hashlib.sha256(active.read_bytes()).hexdigest() in old_defaults:
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
        memory: list[str] | None = None,
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
        if memory:
            notes.extend(memory)
        if history:
            notes.extend(history)
        try:
            prompt = template.format(
                root=root,
                skill_root=skill_root,
                script=skill_root / "scripts" / "solo2611.py",
                notes="\n".join(notes),
            )
            if stage in {"chatgpt", "delivering"}:
                prompt += ("\n本次工作台执行到本地材料核验结束。模板内如有直接提交、查询平台或返修上传的旧指令，"
                           "以工作台统一处理为准。网页录真实窗口，纯终端或应用启动失败录真实终端；"
                           "操作之间留约三秒，结果停留约六秒。")
            return prompt
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
        if "{notes}" not in text:
            errors.append("模板缺少 {notes} 占位符，项目记忆和历史提示词无法自动注入")
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
