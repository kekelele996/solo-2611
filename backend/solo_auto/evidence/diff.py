"""Read-only Git diff evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def diff_summary(workspace: Path, initial_sha: str, artifact_sha: str) -> dict[str, Any]:
    if not initial_sha or not artifact_sha or initial_sha == artifact_sha:
        return {"files": [], "total": {"add": 0, "del": 0, "files": 0}, "short_stat": ""}
    numstat, _, _ = await _git(workspace, "diff", "--numstat", initial_sha, artifact_sha)
    name_status, _, _ = await _git(workspace, "diff", "--name-status", initial_sha, artifact_sha)
    status_map: dict[str, str] = {}
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status_map[parts[-1]] = {
                "A": "added",
                "D": "deleted",
                "M": "modified",
                "R": "renamed",
            }.get(parts[0][:1], "modified")
    files: list[dict[str, Any]] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        deleted = int(parts[1]) if parts[1].isdigit() else 0
        files.append(
            {
                "path": parts[2],
                "add": added,
                "del": deleted,
                "status": status_map.get(parts[2], "modified"),
            }
        )
    short_stat, _, _ = await _git(workspace, "diff", "--shortstat", initial_sha, artifact_sha)
    return {
        "files": files,
        "total": {
            "add": sum(item["add"] for item in files),
            "del": sum(item["del"] for item in files),
            "files": len(files),
        },
        "short_stat": short_stat.strip(),
    }


async def file_diff(
    workspace: Path,
    initial_sha: str,
    artifact_sha: str,
    path: str,
) -> str:
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError("非法 diff 路径")
    text, _, _ = await _git(
        workspace,
        "diff",
        "--no-color",
        "--no-ext-diff",
        initial_sha,
        artifact_sha,
        "--",
        path,
    )
    return text


async def _git(workspace: Path, *args: str) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )
