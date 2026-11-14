from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from solo_auto.dispatch.gates import GateDecision
from solo_auto.events.bus import EventBus
from solo_auto.pipeline.autopilot import Job
from solo_auto.pipeline.breaker import Breaker
from solo_auto.pipeline.intake import IntakeService
from solo_auto.pipeline.prompt_history import PromptHistory, similarity
from solo_auto.settings import AutopilotSettings
from solo_auto.store.repo import Repository
from tests.test_v2 import _app

PROMPT_A = (
    "报送前重算剖析时，上一份结果会先被清空，任务一旦失败，报告单里没有列可看，正在维护的规则也找不到对应列。"
    "希望新剖析整批计算成功后再对外生效，失败时继续使用上一份结果；同一批次重复运行不新增结果。"
)
PROMPT_B = (
    "规则治理页面里，同一条质量规则被多人同时编辑时后保存的人会悄悄覆盖前一个人的阈值，"
    "希望保存时能发现冲突并提示差异，审计记录里保留每次阈值变化和操作人。"
)
PROMPT_A_REWORDED = (
    "重新计算剖析前，上一份结果会先被清空；任务一旦失败，报告里就没有列可看，正在维护的规则也找不到对应的列。"
    "希望新的剖析整批成功后才生效，失败时继续沿用上一份结果；同一批次重复跑不新增结果。"
)


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _write_task(repo: Repository, root: Path, task_id: str, engineering_id: str, prompt: str) -> None:
    (root / "delivery").mkdir(parents=True, exist_ok=True)
    (root / "delivery" / "提示词.md").write_text(prompt, encoding="utf-8")
    repo.execute(
        """
        INSERT INTO tasks(id, task_root, task_name, source_kind, engineering_id, domain, sub_direction,
                          task_type, job_status, exported_at, created_at, updated_at)
        VALUES(?, ?, ?, 'claim', ?, '数据工程', '数仓与数据湖', '功能开发', 'exported', ?, ?, ?)
        """,
        (task_id, str(root), task_id, engineering_id, _now(), _now(), _now()),
    )


def test_similarity_separates_new_questions_from_rewording() -> None:
    ratio, jaccard = similarity(PROMPT_A, PROMPT_B)
    assert ratio < 0.3 and jaccard < 0.2
    ratio, jaccard = similarity(PROMPT_A, PROMPT_A_REWORDED)
    assert ratio >= 0.55 or jaccard >= 0.45


def test_prompt_history_is_scoped_to_the_engineering_and_survives_purge(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "app.db")
    history = PromptHistory(repo, tmp_path / "prompt-history.jsonl")
    try:
        _write_task(repo, tmp_path / "old", "old", "eng-1", PROMPT_A)
        _write_task(repo, tmp_path / "other", "other", "eng-2", PROMPT_A)
        _write_task(repo, tmp_path / "new", "new", "eng-1", PROMPT_A_REWORDED)
        notes = history.notes("new")
        assert len(notes) == 2 and "报送前重算剖析" in notes[1]
        hit = history.duplicate("new", tmp_path / "new")
        assert hit is not None and hit.prior.task_id == "old"
        assert "雷同" in hit.reason

        assert history.backfill() == 3
        repo.execute("DELETE FROM tasks WHERE id = 'old'")
        assert history.duplicate("new", tmp_path / "new") is not None

        (tmp_path / "new" / "delivery" / "提示词.md").write_text(PROMPT_B, encoding="utf-8")
        assert history.duplicate("new", tmp_path / "new") is None
    finally:
        repo.close()


def test_breaker_cools_down_except_for_auth(tmp_path: Path) -> None:
    breaker = Breaker(tmp_path / "breaker.json", EventBus(), AutopilotSettings())
    breaker.trip("gate_rejected", "连续三次门禁失败")
    assert breaker.auto_reset_at
    assert not breaker.maybe_auto_reset()
    breaker.since = (datetime.now().astimezone() - timedelta(minutes=31)).isoformat()
    assert breaker.maybe_auto_reset()
    assert not breaker.tripped and breaker.auto_resets == 1
    breaker.trip("gate_rejected", "又失败")
    breaker.since = (datetime.now().astimezone() - timedelta(minutes=31)).isoformat()
    assert not breaker.maybe_auto_reset(), "第二次自动恢复要等 60 分钟"
    breaker.reset()
    breaker.trip("auth", "401")
    breaker.since = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    assert breaker.auto_reset_at == ""
    assert not breaker.maybe_auto_reset()


class BankSource:
    """A source whose available list shrinks while an engineering is claimed."""

    def __init__(self, ids: list[str]) -> None:
        self.available = list(ids)
        self.list_calls = 0
        self.claimed: list[str] = []

    async def list_engineering(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.list_calls += 1
        items = [{"engineering_id": item, "name": item} for item in self.available]
        return {"items": items, "total": len(items), "page_size": 100}

    async def claim(self, _project_id: str, engineering_id: str) -> dict[str, Any]:
        self.available.remove(engineering_id)
        self.claimed.append(engineering_id)
        return {"claim_id": f"claim-{engineering_id}"}


async def _continuous_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_factory: Any) -> Any:
    monkeypatch.setenv("SOLO_AUTO_DISPATCH__ENGINE", "v3")
    app, _ = await _app(tmp_path, monkeypatch, fixture_factory)
    ctx = app.state.context
    assert ctx.dispatcher.v3_enabled
    ctx.settings.storage.disk_floor_gb = 0
    ctx.dispatcher.config.refill.refill_while_paused = True
    monkeypatch.setattr(IntakeService, "max_in_hand", property(lambda self: 10))
    monkeypatch.setattr(ctx.staging, "list", lambda: [])
    # 这组用例只验证每日上限、空题源退避等计划逻辑；建题节奏由 test_creation_gate 单独覆盖。
    monkeypatch.setattr(
        ctx.dispatcher,
        "creation_decision",
        lambda **_: GateDecision("intake", "create", True),
    )

    async def fake_stage(_source: Any, project_id: str, engineering_id: str, *_: Any, **__: Any) -> dict[str, Any]:
        return {"projectId": project_id, "engineeringId": engineering_id, "name": engineering_id, "claimId": "c"}

    async def fake_create(**kwargs: Any) -> dict[str, str]:
        return {"task_id": f"task-{kwargs['staging_id']}"}

    async def fake_enroll(_task_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(ctx.staging, "stage", fake_stage)
    monkeypatch.setattr(ctx.tasks, "create", fake_create)
    monkeypatch.setattr(ctx.autopilot, "enroll", fake_enroll)
    monkeypatch.setattr(ctx, "test_app", app, raising=False)
    return ctx


def _continuous_plan(daily_cap: int) -> dict[str, Any]:
    return {
        "id": "continuous",
        "project_id": "project-1",
        "project_name": "项目一",
        "mode": "continuous",
        "target": 0,
        "daily_cap": daily_cap,
        "daily": {"date": datetime.now().astimezone().date().isoformat(), "claimed": 0},
        "status": "running",
        "task_ids": [],
        "attempted_engineering_ids": [],
        "claimed_engineering_ids": [],
        "failed_claims": [],
        "claim_limit": 0,
        "filters": {},
        "exclude_delivered": False,
        "max_uses": 0,
    }


@pytest.mark.asyncio
async def test_continuous_plan_keeps_claiming_within_the_daily_cap(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    source = BankSource(["e1", "e2", "e3"])
    ctx.intake.source = source
    ctx.intake.plan = _continuous_plan(daily_cap=2)
    try:
        for _ in range(4):
            await ctx.intake.tick()
        assert source.claimed == ["e1", "e2"]
        assert ctx.intake.plan["status"] == "running"
        assert "今日已领取 2/2" in ctx.intake.plan["waiting_reason"]
        assert ctx.intake.snapshot()["daily"]["claimed"] == 2

        ctx.intake.plan["daily"] = {"date": "2000-01-01", "claimed": 2}
        await ctx.intake.tick()
        assert source.claimed == ["e1", "e2", "e3"]
        assert ctx.intake.plan["daily"]["claimed"] == 1

        ctx.intake._save()
        ctx.intake._load()
        assert ctx.intake.plan["status"] == "running", "持续生产重启后应直接续跑"
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_blocked_history_does_not_hold_continuous_intake(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    source = BankSource(["e1"])
    ctx.intake.source = source
    now = datetime.now().astimezone().isoformat()
    for index in range(20):
        ctx.repo.upsert_task(
            {
                "id": f"historical-blocked-{index}",
                "task_root": str(tmp_path / f"historical-blocked-{index}"),
                "task_name": f"历史阻断任务 {index}",
                "source_kind": "claim",
                "engineering_id": f"historical-{index}",
                "project_id": "project-1",
                "domain": "数据工程",
                "sub_direction": "数仓与数据湖",
                "job_status": "blocked",
                "created_at": now,
                "updated_at": now,
            }
        )
    ctx.intake.plan = _continuous_plan(daily_cap=10)
    try:
        await ctx.intake.tick()
        assert source.claimed == ["e1"]
        assert "backpressure" not in ctx.dispatcher.holds.holds
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_preview_queue_cycles_engineering_without_adjacent_duplicates(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    ctx.intake.source = BankSource(["e1", "e2"])
    ctx.intake.plan = _continuous_plan(daily_cap=10)
    try:
        preview = await ctx.intake.preview_candidates(7)
        assert [item["engineering_id"] for item in preview] == ["e1", "e2", "e1", "e2", "e1", "e2", "e1"]
        assert all(
            preview[index]["engineering_id"] != preview[index - 1]["engineering_id"]
            for index in range(1, len(preview))
        )
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_empty_source_backs_off_and_resumes_by_itself(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    source = BankSource([])
    ctx.intake.source = source
    ctx.intake.plan = _continuous_plan(daily_cap=10)
    try:
        await ctx.intake.tick()
        assert source.list_calls == 1
        assert ctx.dispatcher.holds.holds["source_guard"].reason_code == "source.empty"
        assert ctx.intake.plan["empty_step"] == 1
        await ctx.intake.tick()
        assert source.list_calls == 1, "退避期内不应反复翻题源"

        source.available.append("late")
        ctx.intake.plan["empty_retry_at"] = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
        await ctx.intake.tick()
        assert source.claimed == ["late"]
        assert "source_guard" not in ctx.dispatcher.holds.holds
        assert "empty_step" not in ctx.intake.plan
        assert ctx.intake.plan["status"] == "running"
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_rotation_prefers_least_recently_used_and_failures_expire(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    ctx.intake.source = BankSource(["recent", "stale"])
    monkeypatch.setattr(
        ctx.ledger,
        "engineering_stats",
        lambda **_: [
            {"engineering_id": "recent", "used": 2, "last_used_at": "2026-11-11T10:00:00+08:00"},
            {"engineering_id": "stale", "used": 2, "last_used_at": "2026-11-09T10:00:00+08:00"},
        ],
    )
    plan = _continuous_plan(daily_cap=10)
    try:
        picked = await ctx.intake._next_candidate(plan)
        assert picked is not None and picked["engineering_id"] == "stale"

        plan["failed_claims"] = [{"engineering_id": "stale", "code": "X", "at": _now()}]
        picked = await ctx.intake._next_candidate(plan)
        assert picked is not None and picked["engineering_id"] == "recent"

        old = (datetime.now().astimezone() - timedelta(hours=7)).isoformat()
        plan["failed_claims"] = [{"engineering_id": "stale", "code": "X", "at": old}, {"engineering_id": "recent"}]
        picked = await ctx.intake._next_candidate(plan)
        assert picked is not None and picked["engineering_id"] == "stale"
    finally:
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_disabled_engineering_is_skipped_and_system_failures_do_not_cool(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _continuous_app(tmp_path, monkeypatch, fixture_factory)
    source = BankSource(["e1", "e2"])
    ctx.intake.source = source
    original = ctx.source
    ctx.source = source
    monkeypatch.setattr(ctx.ledger, "engineering_stats", lambda **_: [])
    plan = _continuous_plan(daily_cap=10)
    ctx.intake.plan = plan
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.test_app), base_url="http://test") as client:
            response = await client.put(
                "/api/source/projects/project-1/engineering/e1/disabled",
                json={"disabled": True, "name": "工程一", "reason": "源码太小"},
            )
            assert response.status_code == 200
            listing = (await client.get("/api/source/projects/project-1/engineering")).json()
            flags = {item["engineering_id"]: (item["disabled"], item["disabled_reason"]) for item in listing["items"]}
            assert flags == {"e1": (True, "源码太小"), "e2": (False, "")}
            listing = (await client.get("/api/source/projects/project-1/engineering?scope=all")).json()
            assert {item["holder"] for item in listing["items"]} == {"mine"}, "scope=mine 返回的工程算自己在用"
            assert ctx.intake.snapshot()["disabled_count"] == 1

            picked = await ctx.intake._next_candidate(plan)
            assert picked is not None and picked["engineering_id"] == "e2"
            plan["failed_claims"] = [{"engineering_id": "e2", "code": "AUTOPILOT_BREAKER_OPEN", "at": _now()}]
            picked = await ctx.intake._next_candidate(plan)
            assert picked is not None and picked["engineering_id"] == "e2", "熔断导致的领取失败不应冷却工程"

            await client.put("/api/source/projects/project-1/engineering/e1/disabled", json={"disabled": False})
            assert (await client.get("/api/source/projects/project-1/disabled")).json()["total"] == 0
            plan["failed_claims"] = [{"engineering_id": "e2", "code": "ENGINEERING_ALREADY_CLAIMED", "at": _now()}]
            picked = await ctx.intake._next_candidate(plan)
            assert picked is not None and picked["engineering_id"] == "e1"
    finally:
        ctx.source = original
        await ctx.shutdown()


@pytest.mark.asyncio
async def test_autopilot_refuses_to_export_a_duplicate_prompt(
    fixture_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = await _app(tmp_path, monkeypatch, fixture_factory)
    ctx = app.state.context
    try:
        _write_task(ctx.repo, tmp_path / "runs" / "old", "dup-old", "eng-dup", PROMPT_A)
        _write_task(ctx.repo, tmp_path / "runs" / "new", "dup-new", "eng-dup", PROMPT_A_REWORDED)
        job = Job(task_id="dup-new", task_name="dup-new", stage="exporting")
        ctx.autopilot.jobs[job.task_id] = job
        prompt = ctx.autopilot.prompt_templates.render(
            "chatgpt",
            root=tmp_path / "runs" / "new",
            skill_root=tmp_path,
            missing=[],
            history=ctx.autopilot.prompt_history.notes(job.task_id),
        )
        assert "报送前重算剖析" in prompt
        assert ctx.autopilot._reject_duplicate(job, tmp_path / "runs" / "new")
        assert job.stage == "failed" and job.failure_kind == "prompt_duplicate"
        (tmp_path / "runs" / "new" / "delivery" / "提示词.md").write_text(PROMPT_B, encoding="utf-8")
        assert not ctx.autopilot._reject_duplicate(job, tmp_path / "runs" / "new")
    finally:
        await ctx.shutdown()
