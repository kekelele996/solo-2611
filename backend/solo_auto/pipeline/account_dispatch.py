"""Account-bound intake, source provenance and daily submission capacity."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from solo_auto.account_binding import account_key, origin, standby_key, standby_record, submission_quota_directory
from solo_auto.errors import ApiError
from solo_auto.pipeline.prompt_history import PROMPT_FILE
from solo_auto.pipeline.reaper import reap_and_record
from solo_auto.pipeline.usage import UsageLedger
from solo_auto.settings import SourceSettings
from solo_auto.sources import github
from solo_auto.sources.project_hub import ProjectHubClient
from solo_auto.sources.solo2 import SHANGHAI, DailySubmissions, Solo2Client, submission_day
from solo_auto.sources.staging import StagingManager
from solo_auto.store.state import load_state

if TYPE_CHECKING:
    from solo_auto.context import AppContext

LOGGER = logging.getLogger(__name__)
ROLES = ("primary", "standby")
EARLY_QUOTA = "主号北京时间 20:00 前提交超额，任务已退出调度并留档"
ROLLOVER = "零点已清理副号未开工任务"
SUBMISSION_REFRESH_SECONDS = 60
SUBMISSION_REFRESH_TIMEOUT = 45
SUBMISSION_CACHE_SECONDS = SUBMISSION_REFRESH_SECONDS + SUBMISSION_REFRESH_TIMEOUT


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class Lane:
    route: dict[str, str]
    source: ProjectHubClient
    staging: StagingManager
    ledger: UsageLedger


@dataclass
class SubmissionCount:
    checked_at: datetime
    submissions: DailySubmissions = field(default_factory=DailySubmissions)
    error: str = ""
    checked_receipts: set[str] = field(default_factory=set)


class AccountDispatch:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.repo = ctx.repo
        self._binding = ""
        self._record: dict[str, Any] = {}
        self._standby_source: ProjectHubClient | None = None
        self._primary_owner = ""
        self._primary_check: datetime | None = None
        self._primary_error = ""
        self._stats: dict[tuple[str, str], SubmissionCount] = {}
        self._stats_locks = {role: asyncio.Lock() for role in ROLES}
        self._refresh_task: asyncio.Task[None] | None = None
        self._wake_task: asyncio.Task[None] | None = None
        self._next_release: dict[str, datetime] = {}

    @property
    def binding_key(self) -> str:
        return f"{self.ctx.settings.active_profile}.{standby_key(self.ctx.settings.solo2)}"

    @property
    def secret_key(self) -> str:
        return f"solo2.engineering-pools.{account_key(self.ctx.settings.solo2.base_url, self.ctx.settings.solo2.username)}"

    @property
    def config(self) -> dict[str, Any]:
        key = self.binding_key
        if key != self._binding:
            record = self.ctx.secrets.get_json(self.secret_key, {})
            self._record = record if isinstance(record, dict) else {}
            self._binding = key
        return self._record

    async def invalidate(self) -> None:
        self.ctx.intake.clear_preview()
        self._binding = ""
        self._primary_owner = self._primary_error = ""
        self._primary_check = None
        self._stats.clear()
        source, self._standby_source = self._standby_source, None
        if source:
            await source.close()

    def start(self) -> None:
        self._refresh_task = asyncio.create_task(self._poll_submissions())

    async def shutdown(self) -> None:
        for task in (self._refresh_task, self._wake_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._standby_source:
            await self._standby_source.close()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("primary_project_id") or (
            self.primary_project()[0] and self.ctx.settings.submission.auto_submit and all(self.account("primary"))
        ))

    def primary_project(self) -> tuple[str, str]:
        plan = self.ctx.intake.plan or {}
        if project := plan.get("project_id"):
            return str(project), str(plan.get("project_name") or project)
        project = str(self.config.get("primary_project_id") or "")
        return project, str(self.config.get("primary_project_name") or project)

    async def verify_primary_github(self) -> None:
        if self.config.get("primary_github_owner") or self._primary_owner:
            return
        now = shanghai_now()
        if self._primary_check is None or (now - self._primary_check).total_seconds() >= 60:
            result = await github.test_credentials(self.ctx.secrets.get("github.token"))
            owner = str((result.get("detail") or {}).get("account") or "")
            self._primary_error = str(result["message"])
            if owner and owner.casefold() == str(self.config.get("standby_github_owner") or "").casefold():
                owner = ""
                self._primary_error = "主号和副号必须使用不同的 GitHub 账号"
            self._primary_owner = owner
            self._primary_check = now
        if not self._primary_owner:
            raise ApiError(409, "POOL_GITHUB_IDENTITY", self._primary_error)

    def account(self, role: str) -> tuple[str, str]:
        if role == "primary":
            primary = self.ctx.settings.solo2
            return primary.username.strip(), primary.locked_domain
        standby = standby_record(self.ctx) or {}
        return str(standby.get("username") or ""), str(standby.get("domain") or "")

    def lane(self, role: str) -> Lane | None:
        config = self.config
        account, domain = self.account(role)
        primary_project, primary_name = self.primary_project()
        project = primary_project if role == "primary" else config.get("standby_project_id")
        name = primary_name if role == "primary" else config.get("standby_project_name")
        owner = config.get(f"{role}_github_owner")
        if role == "primary":
            owner = owner or self._primary_owner
        if not (account and project and owner):
            return None
        if role == "primary":
            source = self.ctx.source
            if config.get("primary_github_owner") and (source.settings.username, source.settings.base_url) != (
                config.get("primary_source_user"), config.get("primary_source_address")
            ):
                return None
        else:
            if account != config.get("standby_account") or not primary_project or project == primary_project:
                return None
            if config.get("standby_use_primary_source"):
                source = self.ctx.source
                if (source.settings.username, source.settings.base_url) != (
                    config.get("standby_source_user"), config.get("standby_source_address")
                ):
                    return None
            else:
                if self._standby_source is None:
                    self._standby_source = ProjectHubClient(
                        SourceSettings(username=config["standby_source_user"], base_url=config["standby_source_address"]),
                        self.ctx.secrets, password=config["standby_source_password"],
                    )
                source = self._standby_source
        route = {
            "profileId": self.ctx.settings.active_profile, "primaryAccount": self.ctx.settings.solo2.username.strip(),
            "role": role, "account": account, "domain": domain, "server": origin(self.ctx.settings.solo2.base_url),
            "sourceAddress": source.settings.base_url, "sourceAccount": source.settings.username,
            "projectId": str(project), "projectName": str(name or project),
            "githubOwner": str(owner),
        }
        staging = StagingManager(self.ctx.paths, f"{route['profileId']}-{role}")
        return Lane(route, source, staging, UsageLedger(self.repo, source.settings.base_url))

    def pool_view(self) -> dict[str, Any]:
        project, name = self.primary_project()
        return {**self.config,
                "primary_project_id": project, "primary_project_name": name,
                "primary_source_address": self.ctx.source.settings.base_url}

    def check_project_groups(self, primary: str, standby: str) -> None:
        if primary and primary == standby:
            raise ApiError(422, "POOL_PROJECT_OVERLAP", "主号和副号必须选择不同的项目组")

    def row(self, *, task_id: str = "", claim_key: str = "") -> dict[str, Any] | None:
        field_name, key = ("task_id", task_id) if task_id else ("claim_key", claim_key)
        row = self.repo.query_one(f"SELECT * FROM dispatch_claim_owners WHERE {field_name}=?", (key,))
        if row:
            row["meta"] = json.loads(row["meta"])
        return row

    def owner_rows(self, role: str = "", *, held_only: bool = False) -> list[dict[str, Any]]:
        where = "claim_state='held'" if held_only else "claim_state!='released' OR task_id IS NOT NULL"
        rows = self.repo.query(f"SELECT * FROM dispatch_claim_owners WHERE {where} ORDER BY rowid")
        primary = self.ctx.settings.solo2
        selected = []
        for row in rows:
            if role and row["owner"] != role:
                continue
            row["meta"] = json.loads(row["meta"])
            route = row["meta"].get("accountRoute", {})
            if (route.get("profileId"), route.get("primaryAccount"), route.get("server")) == (
                self.ctx.settings.active_profile, primary.username.strip(), origin(primary.base_url)
            ):
                selected.append(row)
        return selected

    def busy(self, role: str = "") -> bool:
        return any(row["claim_state"] != "released" or self._pending(row) for row in self.owner_rows(role))

    def register_claim(self, lane: Lane, engineering_id: str, claim_id: str, claimed: dict[str, Any], *, day: str) -> dict[str, Any]:
        route = lane.route
        parts = [route[key] for key in ("profileId", "role", "sourceAddress", "sourceAccount", "projectId")]
        key = hashlib.sha256(json.dumps([*parts, engineering_id, claim_id]).encode()).hexdigest()
        meta = {
            "accountRoute": route, "claimKey": key, "claimId": claim_id, "stagingId": key,
            "stagingPath": str(lane.staging.directory(key)), "projectId": route["projectId"],
            "projectName": route["projectName"], "engineeringId": engineering_id,
            "name": str(claimed.get("name") or engineering_id),
        }
        self.repo.execute(
            "INSERT INTO dispatch_claim_owners(claim_key,owner,meta,created_day) VALUES(?,?,?,?)",
            (key, route["role"], json.dumps(meta), day),
        )
        return meta

    async def claim_engineering(self, lane: Lane, engineering_id: str) -> dict[str, Any]:
        day = shanghai_now().date().isoformat()
        project = lane.route["projectId"]
        claimed = await lane.source.claim(project, engineering_id)
        claim_id = str(claimed.get("claim_id") or "")
        meta = self.register_claim(lane, engineering_id, claim_id, claimed, day=day)
        try:
            if not claim_id:
                raise ApiError(409, "CLAIM_ID_REQUIRED", "工程台未返回可核验的领取编号")
            meta = await lane.staging.stage(lane.source, project, engineering_id, claimed, claim_id,
                                            lane.route["projectName"], storage_id=meta["claimKey"], extra=meta)
            self.save_claim(meta)
            lane.ledger.record_claim(meta)
            self.repo.upsert_claim({"id": meta["claimKey"], "project_id": project, "engineering_id": engineering_id,
                                    "engineering_name": meta["name"], "claimed_at": meta["downloadedAt"],
                                    "released_at": None, "task_id": None, "zip_sha256": meta["zipSha256"]})
        except BaseException:
            self.release_later(meta)
            raise
        return meta

    def save_claim(self, meta: dict[str, Any], task_id: str | None = None) -> None:
        self.repo.execute("UPDATE dispatch_claim_owners SET meta=?, task_id=coalesce(?,task_id) WHERE claim_key=?",
                          (json.dumps(meta), task_id, meta["claimKey"]))

    def release_later(self, meta: dict[str, Any]) -> None:
        if meta.get("claimKey"):
            self.repo.execute("UPDATE dispatch_claim_owners SET claim_state='return_pending' WHERE claim_key=? AND claim_state='held'",
                              (meta["claimKey"],))

    async def release(self, meta: dict[str, Any]) -> None:
        row = self.row(claim_key=meta["claimKey"])
        if row is None or row["claim_state"] == "released":
            return
        if row["claim_state"] == "stopping":
            raise ApiError(409, "TASK_STOPPING", "正在终止任务，完成后归还工程")
        meta = row["meta"]
        route = meta["accountRoute"]
        lane = self.lane("standby") if route["role"] == "standby" else None
        source = self.ctx.source if route["role"] == "primary" else (lane.source if lane else None)
        if not source or (self.ctx.settings.active_profile, source.settings.base_url, source.settings.username) != (
            route["profileId"], route["sourceAddress"], route["sourceAccount"]
        ):
            raise ApiError(409, "CLAIM_OWNER_CHANGED", "原题源身份已变化，无法归还工程")
        await source.verify_identity()
        try:
            await source.release(meta["projectId"], meta["engineeringId"], meta["claimId"])
        except ApiError as exc:
            if exc.status_code != 404:
                raise
        with self.repo.transaction() as conn:
            conn.execute("UPDATE dispatch_claim_owners SET claim_state='released' WHERE claim_key=?", (meta["claimKey"],))
            conn.execute("UPDATE claims SET released_at=? WHERE id=?", (shanghai_now().isoformat(), meta["claimKey"]))
        StagingManager(self.ctx.paths, f"{route['profileId']}-{route['role']}").remove(meta["stagingId"])
        if not row["task_id"]:
            UsageLedger(self.repo, source.settings.base_url).mark_released(meta["projectId"], meta["engineeringId"])
        self.repo.add_event("claim.released", f"工程已归还 {meta['projectName']}", actor="intake", task_id=row["task_id"])

    def account_config(self, *, task_root: Path | None = None, claim_key: str = "") -> dict[str, Any]:
        task_root = task_root.resolve() if task_root else None
        task_id = self.repo.task_id_for_root(task_root) if task_root else ""
        row = self.row(task_id=task_id, claim_key=claim_key)
        if row:
            route = row["meta"]["accountRoute"]
            account, domain = self.account(row["owner"])
            if (route["profileId"], route["primaryAccount"], route["server"], route["account"], route["domain"]) != (
                self.ctx.settings.active_profile, self.ctx.settings.solo2.username.strip(),
                origin(self.ctx.settings.solo2.base_url), account, domain,
            ):
                raise ApiError(409, "TASK_OWNER_CHANGED", "任务绑定的提交账号和当前配置不一致")
            if row["dropped_at"]:
                raise ApiError(409, "TASK_REMOVED", row["drop_reason"] or "该任务已退出调度并留档")
            return dict(route)
        if (task_root and (self.enabled or (self.ctx.settings.solo2.username and self.ctx.settings.submission.auto_submit))
                and not self.receipt(task_root).get("submissionId")):
            raise ApiError(409, "TASK_SOURCE_REVIEW", "旧任务缺少可核验的账号领题记录，请使用绑定工程池重新领取")
        return {}

    def audit(self, task_id: str, root: Path, *, starting: bool = False) -> dict[str, Any]:
        try:
            route = self.account_config(task_root=root)
            if not route:
                return route
            row = self.row(task_id=task_id)
            if self.repo.task_id_for_root(root) != task_id:
                raise ApiError(409, "TASK_SOURCE_REVIEW", "任务 ID 与工作目录不一致")
            if not row:
                raise ApiError(409, "TASK_SOURCE_REVIEW", "任务 ID 与工作目录不一致")
            meta = row["meta"]
            task = self.repo.get_task(task_id) or {}
            envelope = load_state(root)
            state = envelope.state
            marker = read_object(root / "monitor/workbench.json")
            expected = {key: meta[key] for key in ("projectId", "engineeringId", "claimKey", "zipSha256", "initialSha")}
            if any(marker.get(key) != value for key, value in expected.items()) or marker.get("accountRoute") != route:
                raise ApiError(409, "TASK_SOURCE_REVIEW", "题源、工程、领取记录或快照与归属凭证不一致")
            if (envelope.raw.get("accountRoute") != route or envelope.raw.get("accountClaim") != meta["claimKey"]
                    or not state or state.initialSha != meta["initialSha"] or task.get("initial_sha") != state.initialSha
                    or state.fields.get("所属领域") != route["domain"]
                    or Path(state.workspace).resolve() != root.resolve() / "workspace"
                    or Path(state.taskRoot).resolve() != root.resolve()
                    or any((root / name).is_symlink() for name in (
                        "workspace", "monitor", "delivery", "monitor/state.json", "monitor/workbench.json"
                    ))
                    or (task.get("project_id"), task.get("engineering_id"), task.get("claim_id")) != (
                        meta["projectId"], meta["engineeringId"], meta["claimKey"]
                    )):
                raise ApiError(409, "TASK_SOURCE_REVIEW", "任务工作目录或初始快照无法核验")
            if state.promptPath and Path(state.promptPath).resolve() != root.resolve() / PROMPT_FILE:
                raise ApiError(409, "TASK_SOURCE_REVIEW", "题面文件必须在该任务的交付目录内")
            paths = (state.session.trace if state.session else "", state.recording.video if state.recording else "")
            if any(value and not Path(value).resolve().is_relative_to(root.resolve()) for value in paths):
                raise ApiError(409, "TASK_SOURCE_REVIEW", "轨迹和录屏必须属于当前任务目录")
            if state.remote.fullName and state.remote.fullName.split("/", 1)[0].casefold() != route["githubOwner"].casefold():
                raise ApiError(409, "TASK_SOURCE_REVIEW", "快照 GitHub 仓库与任务归属不一致")
            if not self.receipt(root).get("submissionId"):
                if reason := self.drop_reason(row):
                    raise ApiError(409, "TASK_REMOVED", reason)
                if row["claim_state"] != "held":
                    raise ApiError(409, "TASK_SOURCE_REVIEW", "任务的工程已归还或正在归还")
            workspace = Path(state.workspace)
            head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=5, check=True)
            clean_ancestry = subprocess.run(["git", "-C", str(workspace), "merge-base", "--is-ancestor", state.initialSha, "HEAD"],
                                             capture_output=True, timeout=5, check=False)
            if Path(head.stdout.strip()).resolve() != workspace.resolve() or clean_ancestry.returncode != 0:
                raise ApiError(409, "TASK_SOURCE_REVIEW", "工作区 Git 历史与任务初始快照不一致")
            if starting:
                self.repo.execute("UPDATE dispatch_claim_owners SET started_at=? WHERE task_id=? AND started_at=''",
                                  (shanghai_now().isoformat(), task_id))
            return route
        except (subprocess.SubprocessError, OSError, KeyError) as exc:
            error = ApiError(409, "TASK_SOURCE_REVIEW", "无法核对任务工作目录及初始快照")
            self.repo.add_event("dispatch.source_audit_failed", error.message, level="error", task_id=task_id)
            raise error from exc
        except ApiError as exc:
            self.repo.add_event("dispatch.source_audit_failed", exc.message, level="error", task_id=task_id)
            raise

    @staticmethod
    def receipt(root: Path) -> dict[str, Any]:
        result = read_object(root / "monitor/submission/api-result.json")
        if result.get("submissionId"):
            return result
        return read_object(root / "monitor/submission/quota-deferred.json") or result

    def priority(self, task_id: str) -> int:
        row = self.row(task_id=task_id)
        return int(bool(row and row["owner"] == "standby"))

    def stopping(self, task_id: str) -> bool:
        row = self.row(task_id=task_id)
        return bool(row and self.drop_reason(row) and not self.receipt(Path(row["meta"]["taskRoot"])).get("submissionId"))

    def _pending(self, row: dict[str, Any]) -> bool:
        if row["dropped_at"] or row["claim_state"] != "held" or self.drop_reason(row):
            return False
        task_id = row["task_id"]
        if not task_id:
            return True
        task = self.repo.get_task(task_id)
        if not task or task.get("presence") == "missing" or task["job_status"] in {"aborted", "archived"}:
            return False
        receipt = self.receipt(Path(task["task_root"]))
        if receipt.get("submissionId"):
            return False
        job = self.ctx.autopilot.jobs.get(task_id)
        queued = self.ctx.dispatcher.queue.get(task_id)
        # 建题后、接入自动交付前同样保留名额。
        return bool(receipt.get("status") == "quota_deferred" or (not job and not queued)
                    or (job and (job.active or job.desktop_running)) or task_id in self.ctx.scheduler.active
                    or self.ctx.scheduler.queue_position(task_id) is not None or task["job_status"] == "running"
                    or (queued and queued.status in {"queued", "launching", "active", "interrupted", "backoff"}))

    def deferred_tasks(self, role: str = "") -> list[dict[str, Any]]:
        deferred = []
        for row in self.owner_rows(role, held_only=True):
            if not row["task_id"] or row["dropped_at"] or self.drop_reason(row):
                continue
            task = self.repo.get_task(row["task_id"])
            if not task or task.get("presence") == "missing" or task["job_status"] in {"archived", "aborted"}:
                continue
            receipt = self.receipt(Path(task["task_root"]))
            route = row["meta"]["accountRoute"]
            if (receipt.get("status") == "quota_deferred" and receipt.get("retryAt")
                    and receipt.get("account") == route["account"] and receipt.get("server") == route["server"]
                    and receipt.get("profile") == route["profileId"] and not receipt.get("submissionId")):
                try:
                    retry_at = datetime.fromisoformat(receipt["retryAt"])
                except (ValueError, TypeError):
                    continue
                deferred.append({"task_id": row["task_id"], "task_name": task["task_name"],
                                 "retry_at": retry_at.isoformat(), "project": row["meta"]["projectName"],
                                 "reason": receipt.get("message") or "等待提交额度恢复"})
        return deferred

    async def refresh(self, role: str, *, force: bool = False) -> None:
        async with self._stats_locks[role]:
            now = shanghai_now()
            account, domain = self.account(role)
            if not account:
                return
            server = self.ctx.settings.solo2.base_url
            day = now.date().isoformat()
            key = (account_key(server, account), day)
            existing = self._stats.get(key)
            if not force and existing and (now - existing.checked_at).total_seconds() < SUBMISSION_CACHE_SECONDS:
                return
            previous = existing.submissions if existing else DailySubmissions()
            binding = self.binding_key
            directory = Path(submission_quota_directory(self.ctx, account))
            local_ids = self._local_submissions(directory, key[0], day, account, self.owner_rows(role))
            settings = self.ctx.settings.solo2.model_copy(update={"username": account, "locked_domain": domain})
            client = self.ctx.solo2 if role == "primary" else Solo2Client(settings, self.ctx.secrets)
            password = None if role == "primary" else str((standby_record(self.ctx) or {}).get("password") or "")
            try:
                async with asyncio.timeout(SUBMISSION_REFRESH_TIMEOUT):
                    current = await client.first_submissions(now.date(), password=password, local_ids=local_ids)
                result = SubmissionCount(shanghai_now(), current, checked_receipts=local_ids)
            except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                result = SubmissionCount(shanghai_now(), previous, f"SOLO2 统计刷新失败（{type(exc).__name__}）",
                                         existing.checked_receipts if existing else set())
            if binding != self.binding_key:
                return
            self._stats[key] = result
            if not existing or (result.submissions, result.error, result.checked_receipts) != (
                previous, existing.error, existing.checked_receipts
            ):
                self.ctx.intake.clear_preview()
                self.ctx.bus.publish("dispatch.quota", {"role": role})

    async def _poll_submissions(self) -> None:
        while True:
            started = shanghai_now()
            try:
                await asyncio.gather(*(self.refresh(role, force=True) for role in ROLES))
            except Exception:
                LOGGER.exception("SOLO2 提交统计刷新失败")
            midnight = datetime.combine(started.date() + timedelta(days=1), time.min, SHANGHAI)
            next_check = min(started + timedelta(seconds=SUBMISSION_REFRESH_SECONDS), midnight)
            delay = (next_check - shanghai_now()).total_seconds()
            await asyncio.sleep(max(0, min(SUBMISSION_REFRESH_SECONDS, delay)))

    def quota(self, role: str, *, include_positions: bool = False) -> dict[str, Any]:
        now = shanghai_now()
        day = now.date().isoformat()
        limit = self.ctx.settings.submission.daily_new_limit
        account, domain = self.account(role)
        account_id = account_key(self.ctx.settings.solo2.base_url, account)
        directory = Path(submission_quota_directory(self.ctx, account))
        marker_path = directory / day
        marker = read_object(marker_path)
        platform_closed = marker.get("reason") == "platform" or (marker_path.is_file() and not marker)
        stats = self._stats.get((account_id, day))
        owner_rows = self.owner_rows(role)
        ids = self._local_submissions(directory, account_id, day, account, owner_rows)
        discarded = set()
        if stats:
            # 本轮读取后才落盘的成功回执先占用名额，历史回执沿用平台核验结果。
            ids = (ids - stats.checked_receipts) | stats.submissions.ids
            discarded = stats.submissions.discarded_ids
        fresh = bool(stats and not stats.error and (now - stats.checked_at).total_seconds() < SUBMISSION_CACHE_SECONDS)
        submitted = len(ids - discarded)
        deferred = self.deferred_tasks(role)
        future = {item["task_id"] for item in deferred if submission_day(item["retry_at"]).isoformat() > day}
        reservations = [row for row in owner_rows if row["task_id"] not in future and self._pending(row)]
        reserved = len(reservations)
        incident = self._incident(role, day)
        platform_closed = platform_closed or bool(incident)
        closed = platform_closed or (fresh and submitted >= limit)
        lane = self.lane(role)
        positions = self._submission_positions(reservations, deferred, submitted) if include_positions and fresh and not platform_closed else {}
        return {
            "account": account, "domain": domain, "open": not closed, "platform_closed": platform_closed,
            "fresh": fresh, "submitted": submitted if fresh or ids else None, "discarded": len(discarded), "reserved": reserved,
            "remaining": 0 if closed else max(0, limit - submitted - reserved) if fresh else None,
            "updated_at": stats.checked_at.isoformat() if stats else "",
            "error": stats.error if stats else "",
            "project": self.primary_project()[1] if role == "primary" else lane.route["projectName"] if lane else "",
            "project_id": self.primary_project()[0] if role == "primary" else lane.route["projectId"] if lane else "",
            "deferred": deferred,
            **({"estimated_positions": positions} if include_positions else {}),
            "quota_directory": str(directory), "limit": limit, "incident_at": incident["observed_at"] if incident else "",
            "stopped": sum(1 for row in owner_rows if row["task_id"] and row["dropped_at"].startswith(day)
                           and row["drop_reason"] == EARLY_QUOTA and row["claim_state"] != "stopping"),
            "stopping": sum(1 for row in owner_rows if row["task_id"] and row["claim_state"] == "stopping"),
        }

    def _local_submissions(self, directory: Path, account_id: str, day: str, account: str,
                           owner_rows: list[dict[str, Any]]) -> set[str]:
        try:
            receipts = json.loads((directory / f"receipts-{day}-{account_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipts = []
        ids = {str(value) for value in receipts} if isinstance(receipts, list) else set()
        for row in owner_rows:
            if root := row["meta"].get("taskRoot"):
                receipt = self.receipt(Path(root))
                stamp = receipt.get("firstSubmittedAt")
                if receipt.get("submissionId") and receipt.get("account") == account and stamp:
                    with suppress(ValueError, TypeError):
                        if submission_day(stamp).isoformat() == day:
                            ids.add(str(receipt["submissionId"]))
        return ids

    def _submission_positions(self, reservations: list[dict[str, Any]], deferred: list[dict[str, Any]],
                              submitted: int) -> dict[str, int]:
        if not reservations:
            return {}
        queue = self.ctx.dispatcher.queue.ordered()
        positions = {item.task_id: index for index, item in enumerate(queue)}
        autopilot = self.ctx.autopilot
        active = ({item.task_id for item in queue if item.active} | self.ctx.scheduler.active.keys()
                  | autopilot.agents.keys() | autopilot.review_tasks.keys())
        retained = {item["task_id"] for item in deferred}

        def order(row: dict[str, Any]) -> tuple[int, int]:
            task_id = row["task_id"]
            job = autopilot.jobs.get(task_id)
            if task_id in retained:
                stage = 0
            elif task_id in active or (job and job.desktop_running):
                stage = 1
            else:
                stage = 2 if task_id else 3
            return stage, positions.get(task_id, len(positions))

        ordered = sorted(reservations, key=order)
        return {row["task_id"]: position for position, row in enumerate(ordered, submitted + 1) if row["task_id"]}

    def selection(self, *, include_staged: bool = False, primary: dict[str, Any] | None = None,
                  standby: dict[str, Any] | None = None) -> tuple[str, str]:
        primary = primary if primary is not None else self.quota("primary")
        if primary["open"] and not primary["fresh"]:
            return "", primary["error"] or "正在核对主号今日提交数"
        if primary["remaining"] or (include_staged and primary["open"] and self.staged("primary")):
            return "primary", "主号优先领取"
        standby = standby if standby is not None else self.quota("standby")
        if not standby["project_id"]:
            return "", "主号预计无可用名额，等待配置副号工程池"
        if standby["open"] and not standby["fresh"]:
            return "", standby["error"] or "正在核对副号今日提交数"
        if standby["remaining"] or (include_staged and standby["open"] and self.staged("standby")):
            reason = "主号今日提交已超额" if primary["platform_closed"] else "主号预计无可用名额"
            return "standby", f"{reason}，副号从专属工程池领题"
        return "", "主号和副号预计均无可用名额"

    def staged(self, role: str) -> dict[str, Any] | None:
        lane = self.lane(role)
        if not lane:
            return None
        for meta in lane.staging.list():
            if meta.get("taskRoot") or meta.get("accountRoute") != lane.route:
                continue
            row = self.row(claim_key=meta.get("claimKey", ""))
            if row and row["meta"] == meta and self._pending(row):
                return meta
        return None

    def _incident(self, role: str, day: str) -> dict[str, Any] | None:
        account, _ = self.account(role)
        return self.repo.query_one("SELECT observed_at FROM dispatch_quota_incidents WHERE account_key=? AND day=?",
                                   (account_key(self.ctx.settings.solo2.base_url, account), day))

    def _observe_rejection(self, role: str, observed: datetime) -> bool:
        day = observed.date().isoformat()
        account = self.account(role)[0]
        if not account:
            return False
        previous = self._incident(role, day)
        if previous and previous["observed_at"] <= observed.isoformat():
            return False
        self.repo.execute("INSERT OR REPLACE INTO dispatch_quota_incidents VALUES(?,?,?)",
                          (account_key(self.ctx.settings.solo2.base_url, account), day, observed.isoformat()))
        self.repo.add_event("dispatch.submission_deferred", "账号新建提交超额，刷新后续调度", actor="skill",
                            details={"role": role, "at": observed.isoformat()})
        return True

    def _sync_rejections(self, role: str, now: datetime) -> None:
        directory = Path(submission_quota_directory(self.ctx, self.account(role)[0]))
        for path in directory.glob("????-??-??"):
            marker = read_object(path)
            if marker.get("reason") != "platform" or not marker.get("observedAt"):
                continue
            with suppress(ValueError, TypeError):
                moment = datetime.fromisoformat(marker["observedAt"])
                local = moment.astimezone(SHANGHAI)
                if moment.tzinfo and local <= now + timedelta(minutes=1) and path.name == local.date().isoformat():
                    self._observe_rejection(role, local)

    def drop_reason(self, row: dict[str, Any]) -> str:
        day = shanghai_now().date().isoformat()
        if row["dropped_at"]:
            return str(row["drop_reason"])
        if row["owner"] == "standby":
            return ROLLOVER if row["created_day"] < day and not row["started_at"] else ""
        # Include the last recorded day so a crash followed by a midnight restart still finishes an early stop.
        account = self.account("primary")[0]
        marker = read_object(Path(submission_quota_directory(self.ctx, account)) / day)
        if marker.get("reason") == "platform" and marker.get("observedAt"):
            with suppress(ValueError, TypeError):
                observed = datetime.fromisoformat(marker["observedAt"])
                local = observed.astimezone(SHANGHAI)
                if observed.tzinfo and local.date().isoformat() == day and local.time() < time(20):
                    return EARLY_QUOTA
        incidents = self.repo.query("SELECT observed_at FROM dispatch_quota_incidents WHERE account_key=? AND day>=? AND day<=?",
                                    (account_key(self.ctx.settings.solo2.base_url, account), row["created_day"], day))
        if any(datetime.fromisoformat(item["observed_at"]).time() < time(20) for item in incidents):
            return EARLY_QUOTA
        return ""

    def notify_deferred(self, role: str, observed_at: datetime, *, account: str, server: str) -> None:
        observed = observed_at.astimezone(SHANGHAI)
        if (account, origin(server)) != (self.account(role)[0], origin(self.ctx.settings.solo2.base_url)):
            raise ApiError(409, "TASK_OWNER_CHANGED", "额度回执的账号不匹配")
        if observed > shanghai_now() + timedelta(minutes=1):
            raise ApiError(422, "QUOTA_TIMESTAMP", "额度回执时间无效")
        self._observe_rejection(role, observed)
        self.ctx.intake.clear_preview()
        self.ctx.bus.publish("dispatch.quota", {"role": role})
        if self.ctx.auto_start and (self._wake_task is None or self._wake_task.done()):
            self._wake_task = asyncio.create_task(self._wake_intake())

    async def _wake_intake(self) -> None:
        try:
            await self.ctx.intake.tick()
        except Exception:
            LOGGER.exception("额度变化后刷新生产计划失败")

    def _create_in_progress(self, role: str) -> bool:
        directory = Path(submission_quota_directory(self.ctx, self.account(role)[0]))
        try:
            with (directory / "create.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            return False
        except BlockingIOError:
            return True
        return False

    async def maintain(self) -> None:
        now = shanghai_now()
        day = now.date().isoformat()
        self.restore_rejections(now)
        rollover = self.repo.query_one("SELECT day FROM dispatch_account_days WHERE binding_key=?", (self.binding_key,))
        if not rollover or rollover["day"] != day:
            self.repo.execute("INSERT OR REPLACE INTO dispatch_account_days(binding_key,day) VALUES(?,?)", (self.binding_key, day))
            self.ctx.intake.clear_preview()
            self.ctx.bus.publish("dispatch.quota", {"date": day})
        work = []
        busy = {role: self._create_in_progress(role) for role in ROLES}
        for row in self.owner_rows():
            if (row["owner"] == "primary" and not row["task_id"] and row["claim_state"] == "held"
                    and (project := self.primary_project()[0]) and row["meta"]["projectId"] != project):
                self.release_later(row["meta"])
                row["claim_state"] = "return_pending"
            reason = self.drop_reason(row) if not row["dropped_at"] and row["claim_state"] in {"held", "return_pending"} else ""
            if reason and not busy[row["owner"]]:
                root = row["meta"].get("taskRoot")
                if not root or not self.receipt(Path(root)).get("submissionId"):
                    row.update(dropped_at=now.isoformat(), drop_reason=reason, claim_state="stopping")
                    self.repo.execute("UPDATE dispatch_claim_owners SET dropped_at=?,drop_reason=?,claim_state=? WHERE claim_key=?",
                                      (row["dropped_at"], reason, "stopping", row["claim_key"]))
            if row["claim_state"] in {"stopping", "return_pending"} and self._next_release.get(row["claim_key"], now) <= now:
                work.append(row)
        await asyncio.gather(*(self._cleanup(row, now) for row in work))

    def restore_rejections(self, now: datetime) -> None:
        for role in ROLES:
            if self.account(role)[0]:
                self._sync_rejections(role, now)

    async def _cleanup(self, row: dict[str, Any], now: datetime) -> None:
        task_id, key = row["task_id"], row["claim_key"]
        try:
            if row["claim_state"] == "stopping":
                if task_id:
                    await self._finish_drop(row)
                self.repo.execute("UPDATE dispatch_claim_owners SET claim_state='return_pending' WHERE claim_key=?", (key,))
            await self.release(row["meta"])
            self._next_release.pop(key, None)
        except Exception as exc:
            self._next_release[key] = now + timedelta(seconds=15)
            message = exc.message if isinstance(exc, ApiError) else str(exc)
            self.repo.add_event("dispatch.account_cleanup_pending", f"任务清理或工程归还待重试：{message}", level="warn", task_id=task_id)

    async def _finish_drop(self, row: dict[str, Any]) -> None:
        task_id, message = row["task_id"], row["drop_reason"]
        root = Path(row["meta"]["taskRoot"])
        await self.ctx.scheduler.quiesce(task_id)
        confirmed = await self.ctx.autopilot.stop_for_quota(task_id, message)
        state = load_state(root).raw
        if row["started_at"] or state.get("attempts") or state.get("pendingContainer"):
            await reap_and_record(self.repo, root, task_id=task_id, actor="dispatch")
            if (root / "monitor/state.json").is_file():
                result = await self.ctx.gateway.call(["cleanup", "--task-root", str(root), "--force", "--preserve-traces"],
                                                     timeout=120, task_id=task_id)
                if result.code != 0:
                    raise ApiError(409, "TASK_STOPPING", result.err.strip() or "容器停止尚未确认")
        if not confirmed:
            raise ApiError(409, "TASK_STOPPING", "等待本题桌面会话停止确认")
        self.ctx.dispatcher.queue.remove(task_id)
        self.repo.set_job_status(task_id, "archived", notes=message)
        self.ctx.ledger.mark_outcome(task_id, "void")
        self.repo.add_event("dispatch.account_task_archived", message, actor="dispatch", task_id=task_id)
        self.ctx.bus.publish("dispatch.quota", {"role": row["owner"], "task_id": task_id})
