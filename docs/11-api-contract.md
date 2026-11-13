# 11 · 内部 API 契约

## 1. 约定

- Base：`http://127.0.0.1:8099`
- 全部 `application/json; charset=utf-8`（除下载类）
- 时间一律 ISO 8601 带时区（`2026-11-11T14:03:22+08:00`）
- 错误统一结构：

```json
{ "error": { "code": "GATE_PROMPT_FAILED", "message": "题面 241 字，超过上限 240", "detail": { "skill_stderr": "..." } } }
```

- `code` 是稳定的机器码，前端按 code 决定交互；`message` 是给人看的；`detail` 可含技能原文。
- 列表接口一律分页：`?page=1&page_size=50`，响应 `{ "items": [], "total": 0, "page": 1, "page_size": 50 }`

## 2. 版本与系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{ok, uptime_s, version}` |
| GET | `/api/version` | `{app, git_sha, skill_version, skill_schema, image, harness_version}` |
| GET | `/api/system/status` | `{docker_ok, disk_free_gb, source_online, offline, running, queued, blocked}` |
| POST | `/api/system/reconcile` | 手动对账，返回 `{corrected: n}` |
| GET | `/api/system/config` | 返回脱敏配置（密钥为 `"<set>"` / `"<unset>"`） |
| PATCH | `/api/system/config` | 部分更新；写 `config.toml`；密钥类字段写 Keychain |
| POST | `/api/system/config/test` | `{target: "skill"\|"source"\|"docker"}` → `{ok, message, detail}` |
| GET | `/api/system/logs?tail=200` | 事件日志，`{items:[{at,level,actor,action,message}]}` |
| POST | `/api/system/backup` | 打包 db + config + drafts，返回路径 |
| POST | `/api/system/prune` | `{dry_run: true, scopes: ["staging","logs"]}` → 将删除项与体积 |

## 3. 题源

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/source/projects` | 项目列表，透传工程台；`?refresh=true` 绕过缓存 |
| GET | `/api/source/projects/{pid}/engineering` | **必须带 pid**；参数 `q, language, min_files, max_files, tag_ids, scope=available&page` |
| GET | `/api/source/projects/{pid}/engineering/{eid}` | 单个工程详情（含 tags、版本、zip_sha256） |
| POST | `/api/claims` | `{project_id, engineering_id}` → `{claim_id, staging_path, sha256_verified}` |
| DELETE | `/api/claims/{claim_id}` | 归还；`?force=false` 时若存在关联未归档任务则 409 |
| GET | `/api/claims` | 已领取未建任务的列表（「待开工」区） |
| GET | `/api/staging` | 本地暂存区内容与占用 |
| GET | `/api/source/prompts?project_id=` | 项目提示词库；汇总该项目的在用任务、历史记录与导入记录 |
| POST | `/api/source/prompts/import` | 项目级导入；`{project_id, source_name?, items:[{text,...}]}` |
| GET | `/api/source/prompts/export?project_id=` | 导出项目提示词库 JSON 文件 |
| POST | `/api/source/prompts/dedupe` | 技能出题前按任务或工程查询历史题面，并检查候选题面是否重复 |

**`GET /api/source/projects/{pid}/engineering` 的行为**：
`pid` 缺失或为空 → `400 {code: "PROJECT_REQUIRED"}`。这是 FR-SRC-02 的服务端强制点。

**提示词库导入**：单次 1–1000 条，单条正文最多 5000 字；支持 `text`、`prompt` 或
`userPrompt` 字段。同一项目内按忽略空白、标点和大小写的题面去重，重复或无效记录跳过，
已有提示词不会被覆盖。导出结构为
`{schema:"solo-auto/prompt-library/1", project:{id,name}, exportedAt, items:[...]}`，
可直接重新导入。每条记录含 `engineeringId` 与 `engineeringName`；名称优先取使用台账，
台账缺失时回退任务名，无工程关联的手工导入归入「未关联工程」。

**提示词去重查询**：请求体优先使用 `{task_root, text?, limit?}` 定位当前任务，也可用
`{project_id, engineering_id, exclude_task_id?, text?, limit?}`。传 `text` 时按技能同一口径返回
`status=duplicate|review|clear`、`blocking`、12 字连续片段以及逐条 `ratio/jaccard/level`；
不传 `text` 时返回该工程历史清单。工作台创建任务时写入 `monitor/workbench.json`，
技能据此调用本接口，不需要自行持有项目或工程 ID。

## 4. 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks` | `?status=&q=&project=&date=&page=`；返回含 11 步摘要 |
| POST | `/api/tasks` | `{staging_id, task_name?, domain?, sub_direction?}` → 调 `init`，返回 `{task_id, task_root, initial_sha}` |
| GET | `/api/tasks/{id}` | 完整详情：任务字段 + steps + 关键 SHA + 状态 |
| GET | `/api/tasks/{id}/steps` | 11 步（供轮询轻量刷新） |
| DELETE | `/api/tasks/{id}` | 仅允许 `aborted` 状态；`?dry_run=true` 返回将删路径 |
| POST | `/api/tasks/{id}/abort` | `{reason}` 必填；不删除文件，只改状态并提示需 `cleanup` |
| POST | `/api/tasks/{id}/archive` | `exported` 后归档 + 归还工程 |
| GET | `/api/tasks/{id}/events` | 该任务的事件流 |

## 5. 题面与快照

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks/{id}/prompt` | `{draft, installed, installed_sha256, readonly, chars, limits}` |
| PUT | `/api/tasks/{id}/prompt/draft` | 保存草稿（写 `drafts/<id>/题面草稿.md`） |
| POST | `/api/tasks/{id}/prompt/preview` | 本地预检：`{ok, chars, errors[], warnings[]}`，**不写文件** |
| POST | `/api/tasks/{id}/prompt/install` | `{task_type, difficulty, languages, repro_level}` → 调技能 `prompt`；返回 `{ok, errors[], warnings[], installed_sha256}` |
| POST | `/api/tasks/{id}/prompt/reopen` | `{reason}` 必填；解除锁定，记录事件 |
| GET | `/api/tasks/{id}/reviews` | 读两个 review json |
| PUT | `/api/tasks/{id}/reviews/{kind}` | `kind=难度论证\|ra-人话审核`；写 `monitor/prompt/*.json` |
| POST | `/api/tasks/{id}/repo` | 调 `repo`；返回 `{ok, initial_sha, permalink, error}` |
| GET | `/api/tasks/{id}/snapshots` | `{initial: {...}, artifact: {...}, ancestor_ok}` |

`prompt/preview` 的本地预检只覆盖能离线判断的规则（字数、禁用词、技术栈词），
**安装时一律以技能返回为准**，两者不一致时以技能为准并在 `detail` 里说明差异。

## 6. 运行

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{id}/run` | 入队；返回 `{state: "queued"\|"running", queue_position}`；已有实例 → `409 {code:"ALREADY_RUNNING"}` |
| POST | `/api/tasks/{id}/run/stop` | v4 调技能 `abort` 保留证据；不再直接杀本地轮询协程 |
| GET | `/api/tasks/{id}/run` | `{state, attempt_seq, attempt_total, phase, round, continues, session_state, next_action_at, log_tail}` |
| GET | `/api/tasks/{id}/run/log?lines=200` | 纯文本（`text/plain`），脱敏后 |
| POST | `/api/tasks/{id}/run/interactive` | 生成启动命令（脱敏），返回 `{command, container_hint}` |
| POST | `/api/tasks/{id}/run/collect` | `interactive` 结束后执行 `collect` |
| POST | `/api/tasks/{id}/reset` | `reset --yes` 前预览；`{confirm: "<task_name>"}` 才执行 |
| GET | `/api/queue` | `{limit, used, slots:[{task_id,pid,container,started_at}], waiting:[...]}` |

执行体的状态（`phase`）值域：`pulling | starting | running | waiting | blocked | collecting |
snapshotting | verifying | done | failed`。终态只由技能的 `run-status` 产生。

## 7. 证据

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{id}/digest` | 生成时间线（调技能 `digest`） |
| GET | `/api/tasks/{id}/timeline` | `{steps:[{no, kind, tool, summary, status, ts, body_ref}], counts:{total, failed}}` |
| GET | `/api/tasks/{id}/timeline/steps/{no}` | 单步完整内容（工具输入/输出），用于展开 |
| GET | `/api/tasks/{id}/diff` | `{files:[{path,add,del,status}], total:{add,del,files}}` |
| GET | `/api/tasks/{id}/diff/file?path=` | 单文件 unified diff（`text/plain`） |
| GET | `/api/tasks/{id}/verify` | 结构校验结果（复用技能 `trace.analyze` 字段） |
| POST | `/api/tasks/{id}/verify` | `{cmd}` 在 workspace 执行，落盘 `monitor/runtime-verification.json` |
| GET | `/api/tasks/{id}/artifacts` | 证据文件清单（kind/path/size/sha/meta） |
| POST | `/api/tasks/{id}/reveal` | `{kind}` → 在 Finder 中定位 |

`verify` 执行用户输入的任意命令 —— 这是**刻意的**（规范要求本机复核），
但必须：只在 `workspace/` 内执行、超时 300s、输出截断 64KB、完整记录到事件与文件。

## 8. 录屏

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/recording/windows` | `{windows:[{id, app, title, width, height, owner_pid}]}`（调技能 `record list`） |
| GET | `/api/tasks/{id}/record/plan` | `{plan, exists, checked:{ok, errors}}` |
| PUT | `/api/tasks/{id}/record/plan` | 保存计划并 `check-plan` |
| GET | `/api/recording/status` | `{recording, task_id, started_at, elapsed_s, remaining_s}` |
| POST | `/api/tasks/{id}/record/start` | `{window_id}` → 启动；全局已有录制 → `409` |
| POST | `/api/recording/stop` | 停止并返回校验结果 |
| GET | `/api/tasks/{id}/record/verify` | `{ok, width, height, duration_s, capture_kind, cursor_ok, errors[]}` |
| POST | `/api/tasks/{id}/record/delete` | 删除不合格录屏（需确认），以便重录 |

`record/start` 的响应必须包含 `{countdown: 3}`，前端据此播放 3-2-1；实际录制在倒计时结束后开始
（由技能侧等待，不是前端等完再调接口 —— 避免网络与渲染延迟导致录制起点偏移）。

## 9. Feedback

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{id}/feedback/init` | 生成模板（调 `feedback --init`） |
| GET | `/api/tasks/{id}/feedback` | `{text, chars, anchors, digits_ratio, coverage:{process,artifact}, checked:{ok,score,errors,warnings}}` |
| PUT | `/api/tasks/{id}/feedback` | 保存正文；**只写 `delivery/feedback.md` 这一处，且内容原样** |
| POST | `/api/tasks/{id}/feedback/check` | 调 `feedback --check`，返回技能原始结果 |
| POST | `/api/tasks/{id}/feedback/ack` | `{acked: true}` → 写 `monitor/review-ack.json`（时间、字数、score） |

**约束**：`PUT /feedback` 不接受任何「生成」「改写」「润色」参数。若前端文本框为空而用户点保存，
保存空内容并让 `check` 报错，**服务端绝不补全文字**。

## 10. 导出

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/export` | `{task_ids:[...]}` 或 `{date:"2026-11-11"}` → `{file, rows, skipped:[{task_id,reason}]}` |
| GET | `/api/exports` | 历史导出记录 |
| GET | `/api/exports/{name}/preview` | 读回 xlsx → `{headers:[...], rows:[[...]]}` |
| GET | `/api/exports/{name}/download` | 文件下载 |
| GET | `/api/stats?from=&to=` | 每日题数、导出数、平均耗时、失败原因分布 |

导出前置检查复用 11 步判定；任一步未完成 → 该题进入 `skipped` 并附原因，不静默丢弃。

**禁止使用 `--allow-incomplete`。** 技能提供该参数是为了人工排障（会生成缺列的 xlsx 且不把
`status` 改为 `exported`），控制台只有在用户显式勾选「预览不完整表（不交付）」时才允许调用，
且生成的表必须打在 `exports/preview/` 下并在文件名里带 `-incomplete`。

## 11. v4 受管会话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{id}/run/resume` | 幂等接回监督器；返回技能 `resume` 与最新 `run-status` |
| POST | `/api/tasks/{id}/run/continue` | 人工发送一次字面“继续”；`{note}` 只写事件日志 |
| POST | `/api/tasks/{id}/run/abort` | 终止会话、保留证据并标记任务 `aborted` |
| GET | `/api/tasks/{id}/run/session` | 轮次、继续、中断、截止时间、监督器状态与 `session_rounds` |
| GET | `/api/dispatch/session-stats` | 继续率、终止原因分布、每容器小时产出 |
| POST | `/api/dispatch/queue/{task_id}/resume` | 从队列侧接回指定任务 |

`GET /api/tasks/{id}/run` 扩展字段 `round`、`continues`、`next_action_at`、`session_state`、
`last_kind`，旧字段保留。继续次数用尽时继续接口返回 `409 SESSION_CONTINUE_REJECTED`。
工作台只轮询技能状态，不自行生成轮次或终止会话。

SSE 新增 `session.round_end`、`session.continue_sent`、`session.held`、`session.aborted`、
`session.accepted`。这些事件只是变化通知，真实数据仍以技能 `state.json` 与 `run-status` 为准。

## 12. 实时通道（SSE）

`GET /api/events/stream`（`text/event-stream`）

```
event: task.updated
data: {"task_id":"...","job_status":"running","step":4,"at":"..."}

event: queue.updated
data: {"limit":4,"used":3,"waiting":2}

event: run.log
data: {"task_id":"...","line":"...","seq":123}

event: toast
data: {"level":"info","message":"gb-544 运行结束","task_id":"..."}

event: system
data: {"docker_ok":true,"disk_free_gb":42,"source_online":true,"offline":false}
```

- `run.log` 每 200ms 合并一次推送（批量行），避免高频事件。
- 客户端断线重连用 `Last-Event-ID`（`id:` 字段为单调递增 seq）。
- **SSE 只用于「通知变化」，不作为数据源** —— 收到事件后前端用 TanStack Query 重新拉对应接口。
  这样即使 SSE 丢事件，界面也能自愈。

## 13. 静态资源

- `GET /` 及所有非 `/api` 路径 → `frontend/dist/index.html`（SPA fallback）
- `GET /assets/*` → 静态文件，带 `Cache-Control: public, max-age=31536000, immutable`
- 开发期由 Vite 5173 代理 `/api` 到 8099

## 14. 状态码约定

| 码 | 含义 | 前端行为 |
|---|---|---|
| 200/201 | 成功 | — |
| 400 | 参数错误 / `PROJECT_REQUIRED` | 就地提示，不弹窗 |
| 401 | 题源会话失效 | 顶部红条 + 「重新登录」 |
| 403 | 权限不足（工程台） | 原文展示 |
| 404 | 资源不存在（任务/工程） | 提示并返回列表 |
| 409 | 状态冲突（已运行/已领取/已安装） | 提示当前真实状态 |
| 422 | 门禁不通过 | 展示 `detail.errors` 原文 |
| 500 | 未预期错误 | 展示 `message` + 「查看日志」 |
| 503 | 离线模式 / Docker 不可用 | 禁用新建类操作 |

## 15. 兼容与演进

- 接口不做版本前缀（单机单前端，同时发布）。若后续需要，加 `/api/v2` 并保留 `/api` 三个月。
- 字段只加不删；删除字段先标记 `deprecated` 并在前端停止使用一个版本。
- 所有响应允许携带未知字段，前端用 zod 做非严格解析。
