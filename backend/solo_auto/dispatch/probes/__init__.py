"""Probe framework for automatically recoverable holds."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from solo_auto.dispatch.facts import FactsCollector
from solo_auto.dispatch.holds import HoldRegistry
from solo_auto.settings import DispatchSettings

ProbeCallable = Callable[[], Awaitable[dict[str, object]]]


@dataclass(slots=True)
class ProbeResult:
    source: str
    ok: bool
    message: str
    detail: dict[str, object]


class ProbeManager:
    def __init__(
        self,
        *,
        holds: HoldRegistry,
        facts: FactsCollector,
        settings: DispatchSettings,
    ) -> None:
        self.holds = holds
        self.facts = facts
        self.settings = settings
        self.llm_probe: ProbeCallable | None = None
        self.source_probe: ProbeCallable | None = None
        self.session_probe: ProbeCallable | None = None
        self.recording_probe: ProbeCallable | None = None
        self.skill_probe: ProbeCallable | None = None
        self._llm_failures = 0
        self._docker_failures = 0
        self._docker_successes = 0
        self._source_failures = 0
        self._source_backoff_step = 0
        self._session_failures = 0
        self._recording_failures = 0
        self._skill_fingerprint = ""

    async def probe(self, source: str) -> ProbeResult:
        if source == "llm_guard":
            return await self._probe_llm()
        if source == "docker_guard":
            return await self._probe_docker()
        if source == "disk_guard":
            return await self._probe_disk()
        if source == "source_guard":
            return await self._probe_source()
        if source == "session_guard":
            return await self._probe_session()
        if source == "recording_guard":
            return await self._probe_recording()
        if source == "skill_guard":
            return await self._probe_skill()
        return ProbeResult(source, False, "该挂起来源没有自动探测器", {})

    async def run_due(self) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        for hold in self.holds.due():
            results.append(await self.probe(hold.source))
        return results

    async def _probe_llm(self) -> ProbeResult:
        if self.llm_probe is None:
            return ProbeResult("llm_guard", False, "网关探测器尚未装配", {})
        result = await self.llm_probe()
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        detail = cast(dict[str, object], result.get("detail")) if isinstance(result.get("detail"), dict) else {}
        if ok:
            self._llm_failures = 0
            self.holds.release("llm_guard", actor="probe")
            return ProbeResult("llm_guard", True, message or "网关可用", detail)
        self._llm_failures += 1
        config = self.settings.llm_guard
        if self._llm_failures >= config.fail_threshold:
            self.holds.register(
                source="llm_guard",
                scope=["containers"],
                reason_code="gateway.unreachable",
                reason=f"网关连续 {self._llm_failures} 次探测失败：{message}",
                probe_seconds=config.paused_probe_seconds,
                self_heal_seconds=3600,
                metadata={"error": message},
            )
        elif "llm_guard" in self.holds.holds:
            self.holds.mark_probe("llm_guard", next_seconds=config.paused_probe_seconds, error=message)
        return ProbeResult("llm_guard", False, message or "网关不可用", detail)

    async def _probe_docker(self) -> ProbeResult:
        facts = await self.facts.snapshot(force=True)
        docker = facts.docker
        if docker.healthy:
            self._docker_successes += 1
            self._docker_failures = 0
            if self._docker_successes >= 2:
                self.holds.release("docker_guard", actor="probe")
            return ProbeResult("docker_guard", self._docker_successes < 2, "Docker 探测成功", {})
        self._docker_successes = 0
        self._docker_failures += 1
        if self._docker_failures >= 3:
            self.holds.register(
                source="docker_guard",
                scope=["containers", "recording"],
                reason_code="docker.unavailable",
                reason=f"Docker 连续 {self._docker_failures} 次探测失败",
                probe_seconds=30,
                self_heal_seconds=900,
                metadata={"error": docker.error},
            )
        return ProbeResult("docker_guard", False, docker.error or "Docker 不可用", {})

    async def _probe_disk(self) -> ProbeResult:
        facts = await self.facts.snapshot(force=True)
        free = facts.disk_free_gb
        floor = self.settings.disk.floor_gb
        resume = floor * self.settings.disk.resume_factor
        if free >= resume:
            self.holds.release("disk_guard", actor="probe")
            return ProbeResult("disk_guard", True, f"磁盘剩余 {free:.1f} GB", {"free_gb": free})
        if free < floor:
            self.holds.register(
                source="disk_guard",
                scope=["containers", "intake"],
                reason_code="disk.low",
                reason=f"磁盘剩余 {free:.1f} GB，低于 {floor:.1f} GB",
                probe_seconds=60,
                self_heal_seconds=1800,
                metadata={"free_gb": free},
            )
        return ProbeResult("disk_guard", False, f"磁盘剩余 {free:.1f} GB", {"free_gb": free})

    async def _probe_source(self) -> ProbeResult:
        if self.source_probe is None:
            return ProbeResult("source_guard", False, "题源探测器尚未装配", {})
        result = await self.source_probe()
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        if ok:
            self._source_failures = 0
            self._source_backoff_step = 0
            existing = self.holds.holds.get("source_guard")
            if existing is None or existing.reason_code != "source.empty":
                self.holds.release("source_guard", actor="probe")
            else:
                return ProbeResult("source_guard", False, "题源可达，但当前筛选仍无候选", {})
            return ProbeResult("source_guard", True, message or "题源可用", {})
        self._source_failures += 1
        intervals = [60, 120, 300, 600, 1800]
        delay = intervals[min(self._source_backoff_step, len(intervals) - 1)]
        self._source_backoff_step += 1
        if self._source_failures >= 3:
            self.holds.register(
                source="source_guard",
                scope=["intake"],
                reason_code="source.unavailable",
                reason=f"题源连续 {self._source_failures} 次不可达：{message}",
                probe_seconds=delay,
                self_heal_seconds=7200,
                metadata={"error": message},
            )
        return ProbeResult("source_guard", False, message or "题源不可用", {})

    async def _probe_session(self) -> ProbeResult:
        if self.session_probe is None:
            return ProbeResult("session_guard", False, "会话探测器尚未装配", {})
        result = await self.session_probe()
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        if ok:
            self._session_failures = 0
            self.holds.release("session_guard", actor="probe")
            return ProbeResult("session_guard", True, message or "会话可用", {})
        self._session_failures += 1
        if self._session_failures >= 2:
            self.holds.register(
                source="session_guard",
                scope=["sessions"],
                reason_code="agent.missing",
                reason=message or "Agent 会话不可用",
                probe_seconds=120,
                self_heal_seconds=1800,
                metadata={"error": message},
            )
        return ProbeResult("session_guard", False, message or "会话不可用", {})

    async def _probe_recording(self) -> ProbeResult:
        if self.recording_probe is None:
            return ProbeResult("recording_guard", False, "录屏探测器尚未装配", {})
        result = await self.recording_probe()
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        if ok:
            self._recording_failures = 0
            self.holds.release("recording_guard", actor="probe")
            return ProbeResult("recording_guard", True, message or "录屏可用", {})
        self._recording_failures += 1
        if self._recording_failures >= 2:
            self.holds.register(
                source="recording_guard",
                scope=["recording"],
                reason_code="recording.permission",
                reason=message or "录屏能力不可用",
                auto=False,
                probe_seconds=300,
                self_heal_seconds=900,
                metadata={"error": message},
            )
        return ProbeResult("recording_guard", False, message or "录屏不可用", {})

    async def _probe_skill(self) -> ProbeResult:
        if self.skill_probe is None:
            return ProbeResult("skill_guard", False, "技能探测器尚未装配", {})
        result = await self.skill_probe()
        ok = bool(result.get("ok"))
        message = str(result.get("message") or "")
        if ok:
            self.holds.release("skill_guard", actor="probe")
            return ProbeResult("skill_guard", True, message or "技能可用", {})
        self.holds.register(
            source="skill_guard",
            scope=["global"],
            reason_code="skill.unavailable",
            reason=message or "技能不可用",
            auto=False,
            probe_seconds=60,
            metadata={"error": message},
        )
        return ProbeResult("skill_guard", False, message or "技能不可用", {})
