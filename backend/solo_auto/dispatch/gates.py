"""Pure admission gate functions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from solo_auto.dispatch.facts import FactsSnapshot
from solo_auto.dispatch.holds import Hold, HoldRegistry
from solo_auto.dispatch.queue import DispatchItem
from solo_auto.settings import DispatchSettings

# 容器与会话的拉起间隔是固定的内部节拍，不再作为可调参数；建题节奏由建题冷却决定。
LAUNCH_SPACING_SECONDS = 20
SESSION_SPACING_SECONDS = 20


@dataclass(slots=True)
class GateResult:
    gate: str
    ok: bool
    code: str = ""
    text: str = ""
    until: str = ""

    @property
    def passed(self) -> bool:
        return self.ok

    def view(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "ok": self.ok,
            "code": self.code,
            "text": self.text,
            "until": self.until,
        }


@dataclass(slots=True)
class GateDecision:
    item_id: str
    stage: str
    allowed: bool
    code: str = ""
    text: str = ""
    until: str = ""
    gate: str = ""
    checks: list[GateResult] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "stage": self.stage,
            "allowed": self.allowed,
            "code": self.code,
            "text": self.text,
            "until": self.until,
            "gate": self.gate,
            "checks": [check.view() for check in self.checks or []],
        }


def evaluate_container_gates(
    item: DispatchItem,
    *,
    settings: DispatchSettings,
    desired_state: str,
    facts: FactsSnapshot,
    holds: HoldRegistry,
    pools: dict[str, dict[str, Any]],
    startup_elapsed: float,
    last_launch_at: float,
    limit_resync_until: str,
    now: datetime,
) -> GateDecision:
    checks = [
        GateResult("G1", settings.enabled, "dispatch.disabled", "调度总开关已关闭") if not settings.enabled else GateResult("G1", True),
        _intent_gate(desired_state),
        _window_gate(settings, now),
        _hold_gate(holds, ["containers"]),
        GateResult("G5", startup_elapsed >= settings.startup_grace_seconds),
        _docker_gate(facts),
        _disk_gate(facts, settings),
        _resync_gate(limit_resync_until, now),
        _capacity_gate(pools.get("containers", {})),
        _spacing_gate(last_launch_at, now),
        _item_gate(item, now),
    ]
    for check in checks:
        if not check.ok:
            return GateDecision(item.task_id, item.stage, False, check.code, check.text, check.until, check.gate, checks)
    return GateDecision(item.task_id, item.stage, True, checks=checks)


def evaluate_creation_gates(
    *,
    settings: DispatchSettings,
    desired_state: str,
    facts: FactsSnapshot,
    holds: HoldRegistry,
    startup_elapsed: float,
    in_hand: int,
    container_used: int,
    last_created_at: float,
    now: datetime,
) -> GateDecision:
    """建题准入，按调度模式只看一个上限，再加严格冷却。

    容器优先：容器占用 + 1 不超过最大容器数（C2），不看最大任务数。
    任务优先：在手任务数小于最大任务数（C1），不看容器占用；容器由容器门禁和技能限流器排队。
    每次判定只放行一道题；冷却从上一次建题开始计时，重启后也接着算，绝不缩短。
    """
    task_limit = settings.max_tasks
    tasks_first = settings.mode == "tasks"
    checks = [
        GateResult("G1", settings.enabled, "dispatch.disabled", "调度总开关已关闭") if not settings.enabled else GateResult("G1", True),
        _intent_gate(desired_state),
        _hold_gate(holds, ["containers"]),
        GateResult("G5", True)
        if startup_elapsed >= settings.startup_grace_seconds
        else GateResult("G5", False, "startup.grace", "服务刚启动，等待状态核对完成"),
        _docker_gate(facts),
        GateResult("C1", True)
        if not tasks_first or in_hand < task_limit
        else GateResult("C1", False, "tasks.full", f"任务已满 {in_hand}/{task_limit}，等在手任务结束再建题"),
        GateResult("C2", True)
        if tasks_first or container_used + 1 <= settings.containers
        else GateResult(
            "C2",
            False,
            "containers.full",
            f"容器已满 {container_used}/{settings.containers}，等容器释放再建题",
        ),
        _cooldown_gate(settings, last_created_at, now),
    ]
    for check in checks:
        if not check.ok:
            return GateDecision("intake", "create", False, check.code, check.text, check.until, check.gate, checks)
    return GateDecision("intake", "create", True, checks=checks)


def _cooldown_gate(settings: DispatchSettings, last_created_at: float, now: datetime) -> GateResult:
    if last_created_at <= 0:
        return GateResult("C3", True)
    cooldown = max(20, settings.create_cooldown_seconds)
    elapsed = now.timestamp() - last_created_at
    if elapsed < 0:
        # 系统时钟回拨时按整段冷却处理，不让冷却被“提前结束”。
        elapsed = 0
    if elapsed < cooldown:
        until = (now + timedelta(seconds=cooldown - elapsed)).replace(microsecond=0).isoformat()
        return GateResult(
            "C3",
            False,
            "create.cooldown",
            f"建题冷却中：距上次建题 {int(elapsed)} 秒，冷却 {cooldown} 秒",
            until,
        )
    return GateResult("C3", True)


def evaluate_session_gates(
    item: DispatchItem,
    *,
    settings: DispatchSettings,
    desired_state: str,
    holds: HoldRegistry,
    pools: dict[str, dict[str, Any]],
    startup_elapsed: float,
    last_session_at: float,
    now: datetime,
) -> GateDecision:
    checks = [
        GateResult("G1", settings.enabled, "dispatch.disabled", "调度总开关已关闭") if not settings.enabled else GateResult("G1", True),
        _intent_gate(desired_state),
        _hold_gate(holds, ["sessions", "authoring", "delivering"]),
        GateResult("G5", startup_elapsed >= settings.startup_grace_seconds),
        _capacity_gate(pools.get("sessions", {})),
        _session_spacing_gate(last_session_at, now),
        _item_gate(item, now),
    ]
    for check in checks:
        if not check.ok:
            return GateDecision(item.task_id, item.stage, False, check.code, check.text, check.until, check.gate, checks)
    return GateDecision(item.task_id, item.stage, True, checks=checks)


def evaluate_agent_gates(
    item: DispatchItem,
    *,
    settings: DispatchSettings,
    desired_state: str,
    holds: HoldRegistry,
    pools: dict[str, dict[str, Any]],
    pool_name: str,
    now: datetime,
) -> GateDecision:
    checks = [
        _intent_gate(desired_state),
        _hold_gate(holds, ["sessions", "authoring", "delivering"]),
        _capacity_gate(pools.get(pool_name, {})),
        _item_gate(item, now),
    ]
    for check in checks:
        if not check.ok:
            return GateDecision(item.task_id, item.stage, False, check.code, check.text, check.until, check.gate, checks)
    return GateDecision(item.task_id, item.stage, True, checks=checks)


def _intent_gate(desired_state: str) -> GateResult:
    if desired_state == "paused":
        return GateResult("G2", False, "intent.paused", "已手动暂停")
    if desired_state == "draining":
        return GateResult("G2", False, "intent.draining", "正在排空后停止")
    return GateResult("G2", True)


def _hold_gate(holds: HoldRegistry, scopes: list[str]) -> GateResult:
    hold = holds.blocking(scopes)
    if hold is None:
        return GateResult("G4", True)
    return GateResult(
        "G4",
        False,
        f"hold.{hold.id}",
        f"已自动挂起：{hold.reason}",
        hold.next_at,
    )


def _docker_gate(facts: FactsSnapshot) -> GateResult:
    if not facts.docker.known:
        return GateResult("G6", False, "docker.unknown", "Docker 状态未确认，暂不启动")
    if not facts.docker.healthy:
        return GateResult("G6", False, "hold.docker_guard", "Docker 暂不可用，等待自动探测")
    return GateResult("G6", True)


def _disk_gate(facts: FactsSnapshot, settings: DispatchSettings) -> GateResult:
    floor = settings.disk.floor_gb
    if facts.disk_free_gb < floor:
        return GateResult(
            "G7",
            False,
            "hold.disk_guard",
            f"磁盘剩余 {facts.disk_free_gb:.0f} GB，低于 {floor:.0f} GB：清出空间后自动继续",
        )
    return GateResult("G7", True)


def _resync_gate(value: str, now: datetime) -> GateResult:
    until = _parse(value)
    if until and until > now:
        return GateResult("G8", False, "limit.resync", "技能侧上限刚被纠正，稍后启动", value)
    return GateResult("G8", True)


_POOL_LABELS = {
    "containers": "容器",
    "sessions": "会话",
    "authoring": "起草",
    "delivering": "交付",
    "recording": "录屏",
    "exporting": "导出",
}


def _capacity_gate(pool: dict[str, Any]) -> GateResult:
    used = int(pool.get("used") or 0)
    limit = int(pool.get("limit") or 0)
    if used >= limit:
        suffix = "，收敛中" if used > limit else ""
        label = _POOL_LABELS.get(str(pool.get("name") or ""), "容器")
        return GateResult("G10", False, "capacity.full", f"{label}已满 {used}/{limit}{suffix}")
    return GateResult("G10", True)


def _spacing_gate(last_launch_at: float, now: datetime) -> GateResult:
    if last_launch_at <= 0:
        return GateResult("G11", True)
    # 严格间隔：连发启动会让桌面深链与容器抢同一秒。
    spacing = LAUNCH_SPACING_SECONDS
    elapsed = now.timestamp() - last_launch_at
    if elapsed < spacing:
        until = (now + timedelta(seconds=spacing - elapsed)).replace(microsecond=0).isoformat()
        return GateResult("G11", False, "spacing", f"距上次启动 {int(elapsed)} 秒，间隔 {spacing} 秒", until)
    return GateResult("G11", True)


def _session_spacing_gate(last_session_at: float, now: datetime) -> GateResult:
    if last_session_at <= 0:
        return GateResult("G11", True)
    elapsed = now.timestamp() - last_session_at
    if elapsed < SESSION_SPACING_SECONDS:
        until = (now + timedelta(seconds=SESSION_SPACING_SECONDS - elapsed)).replace(microsecond=0).isoformat()
        return GateResult(
            "G11",
            False,
            "spacing.session",
            f"距上次拉起会话 {int(elapsed)} 秒，间隔 {SESSION_SPACING_SECONDS} 秒",
            until,
        )
    return GateResult("G11", True)


def _item_gate(item: DispatchItem, now: datetime) -> GateResult:
    if item.status == "quarantined":
        return GateResult("G12", False, "quarantined", item.wait_text or "题目已隔离，稍后自动清理并补位")
    if item.status == "backoff":
        not_before = _parse(item.not_before)
        if not_before and not_before > now:
            seconds = max(0, int((not_before - now).total_seconds()))
            return GateResult("G12", False, "backoff", f"{seconds // 60 + 1} 分钟后第 {item.total_attempts + 1} 次重试", item.not_before)
    if item.wait_code == "duplicate_engineering":
        return GateResult("G12", False, "duplicate_engineering", "同一工程已在队列中")
    return GateResult("G12", True)


def _window_gate(settings: DispatchSettings, now: datetime) -> GateResult:
    window = settings.window
    if not window.enabled or not window.allow:
        return GateResult("G3", True)
    current = now.timetz().replace(tzinfo=None)
    ranges = [_parse_range(value) for value in window.allow]
    for start, end in ranges:
        if start is None or end is None:
            continue
        if _inside(current, start, end):
            return GateResult("G3", True)
    next_start = min((start for start, _ in ranges if start is not None), default=None)
    until = ""
    if next_start is not None:
        target = datetime.combine(now.date(), next_start, tzinfo=now.tzinfo)
        if target <= now:
            target += timedelta(days=1)
        until = target.replace(microsecond=0).isoformat()
    return GateResult("G3", False, "window.closed", "不在允许启动的时段", until)


def _parse_range(value: str) -> tuple[time | None, time | None]:
    try:
        left, right = value.split("-", 1)
        start_hour, start_minute = (int(part) for part in left.split(":"))
        end_hour, end_minute = (int(part) for part in right.split(":"))
        end = time(0, 0) if (end_hour, end_minute) == (24, 0) else time(end_hour, end_minute)
        return time(start_hour, start_minute), end
    except (TypeError, ValueError):
        return None, None


def _inside(value: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= value < end
    return value >= start or value < end


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def first_hold(holds: list[Hold]) -> Hold | None:
    return holds[0] if holds else None
