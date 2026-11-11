# v2 · 02 · 设计说明（架构 / 数据 / 接口 / 界面）

## 1. 架构增量

v1 的分层不变（网关 → 事实层 → 流水线 → API → 前端）。v2 在流水线层增加三个协作者，
全部遵守同一条原则：**状态由磁盘事实推导，调度器只负责「下一步该做什么」，不保存不可重建的状态。**

```
                 ┌──────────────── 生产计划 Intake ────────────────┐
                 │ 目标 N、项目、筛选、在手上限                     │
题源适配器 ◀──── │ 领取 → 下载 → 建题 → enroll                      │
                 └───────────────────────┬─────────────────────────┘
                                         ▼
┌──────────────────────── 自动交付 Autopilot（v1 已有，v2 扩展）────────────────────────┐
│ pending → authoring → running → delivering → exporting → done                          │
│              │            │           │                                                │
│          Agent 进程   scheduler    Agent 进程     ← 阶段记录 / 实时日志 / 失败归类     │
│                                                    ← 熔断器 Breaker（暂停整条线）      │
└───────────────────────────────────────┬────────────────────────────────────────────────┘
                                        ▼
                          ┌──── 抽检 Audit ────┐        ┌──── 提交批次 Batch ────┐
                          │ 抽样 → 待抽检      │ ─通过─▶│ 体检 → export-batch    │
                          │ 通过 / 打回        │        │ 记录批次成员           │
                          └─────────┬──────────┘        └────────────────────────┘
                                    └─打回─▶ 回到 delivering / 转人工 / 作废
```

关键变化：**v1 的 autopilot 在 `delivering` 之后直接导出；v2 把「单题导出」与「进入提交批次」分开。**
单题 xlsx 仍由 autopilot 生成（它是技能完成度的一部分），但提交用的批量表只从
「抽检通过或未被抽中」的集合里生成。

### 1.1 新增模块

| 模块 | 职责 | 备注 |
|---|---|---|
| `pipeline/autopilot.py`（扩展） | 阶段记录、失败归类、从阶段续跑、暂停、停止时清容器 | 常量改为读 `settings.autopilot` |
| `pipeline/agent_stream.py` | 读取 Agent JSONL，脱敏，合帧后发布到事件总线 | 每题 250ms 合并一帧 |
| `pipeline/breaker.py` | 熔断器：按失败类别与抽检打回率计数，触发后置 `paused_by_breaker` | 状态持久化 |
| `pipeline/intake.py` | 生产计划：补位领取、建题、enroll | 同 tick 只领 1 个 |
| `pipeline/audit.py` | 抽样、抽检记录、打回处置 | 抽样结果落盘，重启不重抽 |
| `pipeline/batch.py` | 批次体检、调用 `export-batch`、记录成员 | 表头仍来自技能 |
| `pipeline/janitor.py` | 回收站到期清理、导出后瘦身、孤儿识别 | 每小时一次 |
| `api/{intake,audit,batches,trash}.py` | 对应接口 | 见 §4 |

## 2. 关键取舍（ADR，接 v1 编号）

### ADR-006 · 保留全自动交付，用抽检与熔断对冲（推翻 ADR-004）

- **决策**：自动交付是一等功能。但自动确认的数据在进入提交批次前必须经过抽样策略，
  且连续失败或打回率过高时整条线自动暂停。
- **理由**：ADR-004 担心的是「规则理解偏差导致批量废数」。这个风险没有消失，只是用户选择了产量。
  对冲手段不是回到人工，而是让偏差在 3 题以内被发现，而不是 30 题以后。
- **代价**：抽检占用人工时间（目标 20 题 ≤ 10 分钟）。用户可以把策略设为「不抽检」，
  此时界面在导出页持续显示「本批次含 N 条未经人工复核的数据」。

### ADR-007 · 确认与抽检是两条记录

- **决策**：`review-ack`（谁确认，user / autopilot）保持 v1 语义；新增 `audit`（人工抽检结论）单独存放在
  平台自己的数据目录，不写入技能任务目录。
- **理由**：把人工抽检写成 `ackedBy=user` 会抹掉「这条是 Agent 写的」这个事实。两者分开，
  事后才查得清哪些数据是怎么来的。

### ADR-008 · 抽检不可编辑

- **决策**：抽检页全只读，结论只有通过/打回。
- **理由**：一旦能在抽检页顺手改两个字，平台就成了 Feedback 的编辑器加自动确认器，
  硬规矩 1 名存实亡。要改就打回，由 Agent 或人在任务目录里改，再过一遍技能门禁。

### ADR-009 · 生产计划「补位式」领取，不批量囤积

- **决策**：在手工程数 ≤ 上限，完成一题补一题。
- **理由**：领取是排他且会超时回收的。一次领 20 个，后 15 个大概率在轮到之前就被回收，
  既占别人的坑，也让本地暂存区堆满无效 ZIP。

### ADR-010 · 用 launchd 常驻，不引入进程管理依赖

- **决策**：`make install-service` 生成用户级 LaunchAgent plist；不引入 supervisor、pm2 等。
- **理由**：单机单用户，系统自带机制足够；依赖清单保持不变。

## 3. 数据与配置变更

### 3.1 磁盘布局增量（`~/solo-auto/`）

```
~/solo-auto/
├── autopilot.json            # v1 已有；v2 增加 paused、breaker、每题阶段记录
├── intake.json               # 新：当前生产计划与计数
├── prompts/                  # 新：Agent 提示词模板（authoring.md / delivering.md）
│   └── .defaults/            #     随版本发布的默认模板，用于「恢复默认」
├── audits/<task_id>.json     # 新：抽样结果与抽检记录
├── batches/<batch_id>/       # 新：批次 manifest.json、体检报告、xlsx、（可选）提交包
├── trash/<stamp>-<name>/     # v1 已有；v2 每条增加 meta.json（原路径、大小、到期时间）
└── logs/autopilot/<task_id>/ # v1 已有
```

### 3.2 SQLite 增量

沿用 v1「磁盘为准、SQLite 是索引」的原则，下列表都能从磁盘文件重建。

```sql
-- 自动交付阶段记录（来源：autopilot.json）
CREATE TABLE autopilot_stages (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT NOT NULL,              -- authoring / running / delivering / exporting
  attempt INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  outcome TEXT,                     -- ok / failed / stopped
  failure_kind TEXT,                -- 见 §3.4
  failure_detail TEXT,              -- 原文，截断 4KB
  usage_json TEXT,                  -- Agent 事件里读到的用量，读不到为 NULL
  log_path TEXT
);
CREATE INDEX idx_ap_stages_task ON autopilot_stages(task_id, started_at);

-- 抽检（来源：audits/<task_id>.json）
CREATE TABLE audits (
  task_id TEXT PRIMARY KEY,
  sampled INTEGER NOT NULL,         -- 0 未抽中 / 1 抽中
  verdict TEXT,                     -- NULL 待抽检 / pass / reject
  reason TEXT,
  disposition TEXT,                 -- redo_feedback / manual / void
  decided_at TEXT
);

-- 提交批次（来源：batches/<id>/manifest.json）
CREATE TABLE batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  xlsx_path TEXT,
  check_json TEXT NOT NULL,         -- 体检结果
  unaudited_count INTEGER NOT NULL  -- 批次内未经人工复核的条数
);
CREATE TABLE batch_items (
  batch_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  PRIMARY KEY (batch_id, task_id)
);
```

使用台账是**唯一的例外**：它记录的是历史事实，任务清理后磁盘上已无对应文件，因此以 SQLite 为准，
并由每日备份覆盖（v1 `04` §7）。

```sql
-- 使用台账：一行 = 某工程被使用一次。任务清理/删除时不删除本行。
CREATE TABLE usage_ledger (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,             -- 题源标识（base URL 的规范化形式）
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,       -- 记录当时的名称，题源改名不影响历史
  engineering_id TEXT NOT NULL,
  engineering_name TEXT NOT NULL,
  task_id TEXT,                     -- 不设外键；任务被清理后保留原值
  task_name TEXT,
  claimed_at TEXT NOT NULL,
  task_created_at TEXT,             -- NULL = 领取未用
  delivered_at TEXT,                -- 单题导出完成时间
  submitted_at TEXT,                -- 首次进入提交批次时间
  outcome TEXT NOT NULL,            -- claimed / in_progress / delivered / submitted / failed / void / released
  failure_kind TEXT,
  purged_at TEXT,
  backfilled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_usage_project ON usage_ledger(source, project_id);
CREATE INDEX idx_usage_eng ON usage_ledger(source, engineering_id);
```

写入时机：领取成功插入一行；建题、单题导出完成、进入批次、失败、作废、归还、清理时更新同一行。
三个次数都是对该表的聚合：使用 = `task_created_at IS NOT NULL`，交付 = `delivered_at IS NOT NULL`，
提交 = `submitted_at IS NOT NULL`。本地目录题源（非工程台）以目录路径作 `engineering_id`。

`tasks` 表增加三列：`ack_by TEXT`、`skill_version TEXT`、`presence TEXT DEFAULT 'present'`
（`present` / `missing`）。迁移方式同 v1（启动时 `PRAGMA user_version` 递增，纯 `ALTER TABLE ADD COLUMN`）。

### 3.3 配置增量（`config.toml`）

```toml
[autopilot]
enabled = true
agent_bin = ""                 # 空 = 自动探测 codex
agent_args = []                # 追加参数，例如模型选择
restricted = false             # FR-AP-12
stage_timeout_seconds = 3600
stage_attempts = 2
run_attempts = 2
parallel_authoring = 2
parallel_delivering = 1
stop_kills_containers = true

[autopilot.breaker]
same_kind_failures = 3
audit_reject_rate = 0.2        # 最近 10 次抽检
audit_reject_window = 10

[audit]
mode = "sample"                # all / sample / off
sample_rate = 0.3
stratify_by = "task_type"

[intake]
max_in_hand = 0                # 0 = 起草并发 + 容器并发
exclude_tags = []

[trash]
retention_days = 7

[slim]
enabled = false
after_days = 3
patterns = ["node_modules", ".venv", "dist", "build", "target", "__pycache__"]

[notify]
macos = true
webhook_url = ""               # 存 Keychain，不写明文
```

每一项在设置页都标注生效方式（FR-PLAT-04）：`autopilot.*`、`audit.*`、`intake.*` 下次阶段/下次抽样生效；
`trash.*`、`slim.*` 下次清理周期生效；`notify.*` 立即生效。

### 3.4 失败类别（权威枚举）

| `failure_kind` | 判定依据 | 默认处置 |
|---|---|---|
| `agent_timeout` | 阶段超时被终止 | 可重试 |
| `agent_reported` | Agent 末行 `AUTOPILOT: FAILED <原因>` | 可重试；原因原文展示 |
| `gate_rejected` | 技能门禁返回 errors | 可重试；errors 原文展示 |
| `run_incomplete` | 运行结束但 1–8 步未过 | 用尽 `run_attempts` 后不再重试，建议换题 |
| `auth` | 运行或 Agent 输出命中鉴权失败 | **立即熔断**，不重试 |
| `recording` | `record auto` 失败或隐私门禁未过 | 可重试 1 次 |
| `export` | 导出失败 | 可重试 |
| `environment` | Docker 不可用、磁盘不足、Agent 不存在 | **立即熔断** |

### 3.5 自动交付状态机（v2）

```
pending ─▶ authoring ─▶ running ─▶ delivering ─▶ exporting ─▶ awaiting_audit ─▶ done
              │            │            │             │              │
              └────────────┴────────────┴─────────────┴──▶ failed    └─ reject ─▶ delivering / manual / void
任意非终态 ─▶ stopped（用户停止）      任意非终态 ─▶ paused（用户暂停或熔断，可继续）
```

- `awaiting_audit` 只在该题被抽中时出现；未抽中直接 `done`。
- 「从阶段续跑」不是把状态改回去，而是清掉该阶段的尝试计数后让 `_advance` 重新由磁盘事实推导。
  因此不存在「跳过门禁续跑」的可能。
- 打回处置 `redo_feedback`：删除平台侧的自动确认记录，调用技能已有的重置 Feedback 能力（若技能无此命令，
  见 Q-V2-2），回到 `delivering`。

## 4. API 增量

约定同 v1 `11`：JSON、错误体 `{code, message, detail}`、校验失败 400、变更类接口要求同源头。

### 4.1 自动交付

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/autopilot` | v1 已有；响应增加 `paused`、`breaker{tripped, kind, since}`、每题 `stages[]`、`failure_kind`、`audit` |
| POST | `/api/autopilot/preflight` | 返回预检项 `[{key, level, ok, message, fix}]` |
| POST | `/api/autopilot/start` | v1 已有；预检 P0 项不过返回 409 `PREFLIGHT_FAILED` |
| POST | `/api/autopilot/stop` | 增加 `kill_containers: bool` |
| POST | `/api/autopilot/pause` · `/resume` | 全局暂停/继续；`resume` 同时复位熔断 |
| POST | `/api/autopilot/{task_id}/retry` | body `{from_stage?: string}`；不可选的阶段返回 409 并说明原因 |
| GET | `/api/autopilot/{task_id}/stages` | 阶段记录 |
| GET | `/api/autopilot/{task_id}/log` | v1 已有；增加 `stage`、`download=1` |
| GET | `/api/autopilot/prompts` | 模板名、路径、修改时间、内容（只读） |

SSE 新事件：`autopilot.log {task_id, stage, frames[]}`、`autopilot.breaker {tripped, kind}`。

### 4.2 生产计划

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/intake` | 当前计划与计数、预计完成时间 |
| POST | `/api/intake` | `{project_id, filters, target}`；已有进行中的计划返回 409 |
| POST | `/api/intake/pause` · `/resume` · `/stop` | `stop` 可带 `release_unstarted: bool` |

### 4.3 抽检

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audits?verdict=pending` | 抽检队列 |
| GET | `/api/audits/{task_id}` | 审阅所需的聚合只读数据 |
| POST | `/api/audits/{task_id}` | `{verdict: "pass"\|"reject", reason?, disposition?}`；`reject` 缺 `reason` 返回 400 |
| GET | `/api/quality/summary?from=&to=` | 质量看板统计 |
| POST | `/api/recordings/privacy-recheck` | `{task_ids}`，逐题返回结果或「无法检查」 |

### 4.4 批次、回收站、平台

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/batches/check` | `{task_ids}` → 体检报告，不产生文件 |
| POST | `/api/batches` | 体检有阻断项时返回 409 并附报告；成功返回批次 |
| GET | `/api/batches` · `/api/batches/{id}` | 列表与详情（成员、报告、文件） |
| POST | `/api/batches/{id}/bundle` | 生成提交包 |
| GET | `/api/trash` | 条目列表 |
| POST | `/api/trash/{entry}/restore` | 原路径被占用返回 409 |
| DELETE | `/api/trash/{entry}` · `/api/trash` | 删除单条 / 清空 |
| POST | `/api/tasks/{id}/forget` | 仅 `presence=missing` 可用 |
| GET | `/api/version` | 增加 `build_id` |
| POST | `/api/system/restart` | 有运行中容器/录制且未带 `force` 时返回 409 并列出 |
| POST | `/api/system/diagnostics` | 生成并返回诊断包路径 |

### 4.5 使用统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/usage/projects?from=&to=` | 按项目汇总：`used` `delivered` `submitted` `claimed_unused` `delivery_rate` `last_used_at` |
| GET | `/api/usage/projects/{project_id}/engineering` | 该项目下各工程的同组指标 |
| GET | `/api/usage/engineering/{engineering_id}` | 该工程的逐次使用记录 |
| GET | `/api/usage/export.csv` | CSV 导出 |

题源页现有的工程列表接口在每一项上附带 `usage{used, delivered, submitted, last_outcome, last_failure_kind}`，
由后端按 `engineering_id` 关联台账得出；题源的服务端筛选与分页不变，「未使用过」筛选在返回页内标注，
不改变服务端分页语义（界面注明「仅筛当前页」）。

## 5. 界面与交互

沿用 v1 `05`–`08` 的令牌、密度、动效白名单与快捷键模型；v2 不新增动效，不新增依赖。

### 5.1 信息架构变化

```
左导航（v1）：看板 / 任务 / 题源 / 导出 / 设置
左导航（v2）：看板 / 生产线 / 任务 / 抽检(n) / 题源 / 导出 / 设置
                     └ 新          └ 新，角标 = 待抽检数
回收站：设置页内的一个分区 + 命令面板直达，不占一级导航
```

跳转键追加：`g p` 生产线、`g a` 抽检。

### 5.2 看板：收件箱优先

```
┌─ 待我处理 (3) ──────────────────────────────────────────────────────────┐
│ ⛔ 自动交付已暂停：连续 3 题「门禁未过」          [查看原因] [继续]     │
│ ☐ 待抽检 6 题                                     [开始抽检]            │
│ ⚠ 题源登录已过期，生产计划暂停                    [重新登录]            │
└─────────────────────────────────────────────────────────────────────────┘
┌─ 今日计划  目标 20 ─────────────────────────────────────────────────────┐
│ ████████████░░░░░░░░  完成 11 · 进行中 5 · 失败 2 · 预计 18:40 完成      │
└─────────────────────────────────────────────────────────────────────────┘
（以下为 v1 原有分组）
```

规则：收件箱每条只有一个主操作；没有待办时整块收成一行「没有需要你处理的事」。

### 5.3 生产线页

```
起草 (2)        运行 (4)        交付 (1)        待抽检 (6)      完成 (11)     失败 (2)
┌───────────┐  ┌───────────┐   ┌───────────┐   ┌───────────┐                ┌──────────────┐
│ gb-551    │  │ gb-548    │   │ gb-544    │   │ gb-540    │                │ gb-539       │
│ 12:04 · 1 │  │ 31:20 · 1 │   │ 04:11 · 2 │   │ 已抽中    │                │ 门禁未过 · 2 │
│ ▸ 日志    │  │ ▸ 日志    │   │ ▸ 日志    │   │ ▸ 审阅    │                │ ▸ 重试 ▾     │
└───────────┘  └───────────┘   └───────────┘   └───────────┘                └──────────────┘
```

- 卡片第二行：阶段已用时 · 尝试次数；失败列显示失败类别。
- 展开日志在卡片下方内联打开，实时滚动；用户向上滚动后停止自动跟随，出现「回到最新」。
- 失败卡的「重试 ▾」列出当前可选阶段；不可选的阶段置灰并写明原因（例如「已有会话，不能重写题面」）。
- 顶部工具条：`今天交付 N 题`、`暂停/继续`、`全部停止`、失败类别筛选、当前配置摘要（并发、超时，点击跳设置）。
- 列内超过 8 张卡时折叠为「另有 n 题」，不出现横向滚动。

### 5.4 抽检页

```
┌ 队列 6 ──┐┌ 题面 ─────────────────────────────┐┌ Feedback ───────────────────┐
│▸gb-540   ││ （只读，字数 162/240）            ││ 交付完整性 3                │
│ gb-541   │├ 轨迹摘要 ─────────────────────────┤│ （正文只读）                │
│ gb-543   ││ 87 步 · 失败 4 · 最终 end_turn    ││ 字数 236 · 锚点 5 · 检查通过│
│ ...      ││ ▸ 第 41 步 pytest 失败 …          │├ 录屏 ───────────────────────┤
│          │├ 产物 ─────────────────────────────┤│ ▶ 00:00 / 01:12  720p       │
│          ││ 9 文件 +412 −37                   ││ 隐私检查：通过              │
└──────────┘└───────────────────────────────────┘└─────────────────────────────┘
 [a] 通过    [r] 打回…    [ / ] 上一题/下一题    [Space] 播放    [o] 打开任务详情
```

- 打回弹层：原因（必填）+ 处置三选一。提交后自动进入下一题。
- 队列为空时显示「全部抽检完毕」与「去导出」按钮。
- 页面没有任何输入框可以修改题面、Feedback 或分数。

### 5.5 导出页

- 顶部新增「生成提交批次」，先展示体检报告：阻断项（重复 ID、文件缺失、被抽中但未通过）红色并禁止生成；
  提示项（相似度告警、未经人工复核 N 条）黄色，需勾选「我已知晓」后可继续。
- 批次历史列表：时间、条数、未复核条数、文件；每个批次可展开成员。
- 已进入过批次的任务默认不在候选里，勾选「包含已导出」后出现并带「重复导出」标记。

### 5.6 设置页

新增分区：自动交付、抽检、生产计划、通知、回收站与瘦身、服务（安装常驻 / 重启后端 / 诊断包）。
每个字段右侧一个小标签：`立即` / `下次任务` / `需重启`。保存 Toast 文案按实际生效方式拼出，
例如「已保存。并发设置将在下一个阶段开始时生效。」

回收站分区：条目表（原任务名、清理时间、大小、剩余天数）+ 恢复 / 删除 / 清空。

### 5.7 全局

- 版本不一致横幅固定在顶栏下方，不可关闭，带「重启后端」按钮（未装常驻服务时改为显示重启命令并可复制）。
- 系统通知点击后聚焦浏览器并跳到对应任务或收件箱。
- 任务列表行内标记：`自动`（ackedBy=autopilot）、`待抽检`、`已抽检`、`旧版`、`目录缺失`。
- 「旧版遗留」分组默认折叠在列表末尾，组头提供「全部清理」。

### 5.8 使用统计

入口：导出页下的「使用统计」页签 + 命令面板 `> 使用统计`；题源页内联显示。

```
使用统计                                   [近 7 天 ▾]  [导出 CSV]      统计自 2026-11-11 起完整
项目              使用   交付   提交   交付率   领取未用   最近使用
▾ 数据工程-A       18     14     12     78%       1        今天 14:02
    etl-orders      3      2      2     67%       0        今天 14:02
      ├ gb-551  11-11 14:02  已提交
      ├ gb-532  11-10 16:40  已交付
      └ gb-520  11-10 09:15  失败 · 运行结构不完整（已清理）
    stream-join     1      1      1    100%       0        昨天
▸ 数据工程-B        6      3      3     50%       0        昨天
```

- 题源页工程表新增三列窄数字「用 / 交 / 提」，0 显示为 `–`；使用过但从未交付的工程行尾带提示色标记。
- 领取确认弹窗在后果说明之上加一行历史（FR-STAT-04）；从未使用过的工程不显示该行。
- 已清理任务的记录保留并标「已清理」，不可点击跳转。

### 5.9 效率指标（续 v1 `08` §1）

| 指标 | v1 目标 | v2 目标 |
|---|---|---|
| 无人值守时的人工操作数 / 20 题 | —— | ≤ 3（下计划、抽检、生成批次） |
| 单题抽检耗时 | —— | ≤ 90 秒 |
| 失败到再次推进的操作数 | 重新走流程 | 1 次点击 |
| 发现「线停了」的延迟 | 回到页面才知道 | ≤ 1 分钟（系统通知） |
