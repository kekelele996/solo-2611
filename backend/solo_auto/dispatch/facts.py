"""Read-only facts snapshot used by all v3 admission decisions."""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from solo_auto.dispatch.faults import FaultController

MANAGED_PREFIX = "solo2611-"


def _managed_container_names(text: str) -> set[str]:
    """Count labelled containers and legacy names even if a label was lost."""
    names: set[str] = set()
    for raw in (text or "").splitlines():
        name, _, label = raw.partition("\t")
        name = name.strip()
        if name and (name.startswith(MANAGED_PREFIX) or label.strip().lower() == "true"):
            names.add(name)
    return names


@dataclass(slots=True)
class DockerFacts:
    known: bool
    healthy: bool
    containers: frozenset[str] = field(default_factory=frozenset)
    observed_at: float = 0.0
    error: str = ""
    degraded: bool = False


@dataclass(slots=True)
class FactsSnapshot:
    docker: DockerFacts
    disk_free_gb: float
    observed_at: float
    processes: tuple[tuple[int, int, str], ...] = ()

    @property
    def docker_age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.docker.observed_at) if self.docker.observed_at else float("inf")


class FactsCollector:
    def __init__(
        self,
        home: Path,
        *,
        timeout_seconds: float = 20,
        stale_seconds: float = 15,
        faults: FaultController | None = None,
    ) -> None:
        self.home = home
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.faults = faults or FaultController()
        self._docker = DockerFacts(known=False, healthy=False)
        self._snapshot: FactsSnapshot | None = None
        self._lock = asyncio.Lock()

    async def snapshot(self, *, force: bool = False) -> FactsSnapshot:
        async with self._lock:
            now = time.monotonic()
            if not force and self._snapshot and now - self._snapshot.observed_at < 2.0:
                return self._snapshot
            if force or now - self._docker.observed_at >= 2.0:
                self._docker = await self._probe_docker()
            free = self._disk_free()
            processes = await self._processes_cached(now)
            self._snapshot = FactsSnapshot(
                docker=self._docker,
                disk_free_gb=free,
                observed_at=now,
                processes=processes,
            )
            return self._snapshot

    async def _probe_docker(self) -> DockerFacts:
        spec = self.faults.get("docker.ps")
        await self.faults.pause("docker.ps")
        if spec.fail:
            return DockerFacts(
                known=False,
                healthy=False,
                observed_at=time.monotonic(),
                error="injected docker failure",
            )
        if isinstance(spec.value, list):
            return DockerFacts(
                known=True,
                healthy=True,
                containers=frozenset(str(item) for item in spec.value),
                observed_at=time.monotonic(),
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--format",
                '{{.Names}}\t{{.Label "solo2611"}}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except (OSError, TimeoutError) as exc:
            return DockerFacts(
                known=False,
                healthy=False,
                observed_at=time.monotonic(),
                error=str(exc) or "docker ps timeout",
            )
        if proc.returncode != 0:
            degraded = await self._degraded_probe()
            return DockerFacts(
                known=degraded is not None,
                healthy=degraded is not None,
                containers=frozenset(degraded or ()),
                observed_at=time.monotonic(),
                error=stderr.decode("utf-8", errors="replace").strip() or "docker ps failed",
                degraded=degraded is not None,
            )
        values = frozenset(
            _managed_container_names(stdout.decode("utf-8", errors="replace"))
        )
        return DockerFacts(known=True, healthy=True, containers=values, observed_at=time.monotonic())

    async def _degraded_probe(self) -> set[str] | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--format",
                '{{.Names}}\t{{.Label "solo2611"}}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except (OSError, TimeoutError):
            return None
        return (
            _managed_container_names(stdout.decode("utf-8", errors="replace"))
            if proc.returncode == 0
            else None
        )

    def _disk_free(self) -> float:
        spec = self.faults.get("disk.free")
        if isinstance(spec.value, int | float):
            return float(spec.value)
        self.home.mkdir(parents=True, exist_ok=True)
        return round(shutil.disk_usage(self.home).free / 1024**3, 2)

    async def _processes_cached(self, now: float) -> tuple[tuple[int, int, str], ...]:
        snapshot = self._snapshot
        if snapshot and now - snapshot.observed_at < 15:
            return snapshot.processes
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-axo",
                "pid=,pgid=,command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (OSError, TimeoutError):
            return ()
        result: list[tuple[int, int, str]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            try:
                result.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
        return tuple(result)


def local_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).astimezone().replace(microsecond=0).isoformat()


async def docker_running_containers(
    *,
    managed_only: bool = False,
    timeout_seconds: float = 20,
) -> set[str] | None:
    args = ["docker", "ps"]
    if managed_only:
        args.extend(["--filter", "label=solo2611=true"])
    args.extend(["--format", "{{.Names}}"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return set(stdout.decode("utf-8", errors="replace").split())


async def docker_container_alive(name: str, *, timeout_seconds: float = 20) -> bool:
    args = ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except (OSError, TimeoutError):
        return False
    return proc.returncode == 0 and name in stdout.decode("utf-8", errors="replace").split()


async def docker_info_ok(*, timeout_seconds: float = 20) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            "--format",
            "{{.ServerVersion}}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
    except (OSError, TimeoutError):
        return False
    return proc.returncode == 0
