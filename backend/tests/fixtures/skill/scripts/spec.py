"""Minimal test double for the real skill-owned schema."""

from __future__ import annotations

import re

SUBMIT_COLUMNS = [
    "User Prompt",
    "所属领域",
    "子方向",
    "任务类型",
    "任务难度",
    "语言/框架",
    "Harness",
    "Harness 版本",
    "操作系统",
    "环境可复现等级",
    "初始环境快照",
    "SessionID",
    "TurnID/PromptID",
    "轨迹文件",
    "产物快照",
    "运行录屏",
    "交付完整性",
    "Feedback",
    "备注",
]

DOMAINS = {"数据工程": ["其他"]}
TASK_TYPES = ["功能开发", "Bugfix"]
DIFFICULTIES = ["困难", "地狱"]
REPRO_LEVELS = ["无外部依赖", "已容器化，可一键起环境"]
ANCHOR_PATTERN = re.compile(r"(`[^`]+`|[\w.-]+\.py\b)")


def scan_banned(text: str, domain: str = "") -> tuple[list[str], list[str]]:
    del text, domain
    return [], []
