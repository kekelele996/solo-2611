# 04 · 数据存储

## 1. 分层

```
事实源（唯一真相）        索引（可删可重建）        配置（人可改）
─────────────────         ─────────────────         ──────────────
TASK_ROOT/                solo-auto.db              config.toml
  └ monitor/state.json      ├ tasks                 └ Keychain（密钥）
  └ delivery/*              ├ steps
  └ recording/*             ├ events
  └ traces/*                ├ artifacts
                            └ claims
```

**任何写入顺序**：先让技能写磁盘 → 再更新索引。索引写失败不影响事实；事实丢失则索引必须能重建。

## 2. 磁盘布局

```
~/solo-auto/                        # APP_HOME，可配置
├── config.toml                     # 人可编辑
├── solo-auto.db                    # SQLite 索引
├── run/
│   ├── containers.lock             # 容器槽位 flock
│   ├── slots/<uuid>.json           # 每个占位：{taskId, pid, container, startedAt}
│   ├── recording.lock              # 录屏互斥
│   └── solo-auto.pid               # 单实例锁
├── staging/                        # 领取后未建任务的工程
│   └── <engineeringId>/
│       ├── meta.json               # {engineeringId, projectId, name, versionId, claimId, zipSha256, tags, downloadedAt}
│       ├── source.zip
│       └── source/                 # 解包目录，作为 init --source
├── tasks/                          # 技能默认 tasksDir（可改成外部路径）
│   └── 2026-11-11-gb-544-xxxx/
│       ├── workspace/              # bind mount 到容器 /workspace
│       ├── traces/-workspace/<sessionId>.jsonl
│       ├── recording/运行录屏.mp4
│       ├── delivery/{题面草稿.md,提示词.md,轨迹时间线.md,feedback.md,交付表.xlsx,字段说明.md}
│       └── monitor/{state.json,record-plan.json,prompt/*,runs/*,feedback-check.json}
├── drafts/                         # 控制台侧草稿（不污染技能目录）
│   └── <taskId>/{题面草稿.md, difficulty-review.json, ra-renhua-review.json, feedback.md}
├── exports/
│   └── 交付表-2026-11-11.xlsx
└── logs/
    ├── app.log                     # 滚动 20MB × 5
    └── skill/<taskId>/<cmd>-<ts>.log
```

### 设计说明

- **`drafts/` 与技能目录分离**：题面草稿在控制台里编辑，点「安装」时才由技能写 `delivery/提示词.md`。
  这样「改了但没装」的状态是显式的，不会出现「文件变了但门禁没跑」。
  > 实现方式：技能的 `prompt --init` 默认把草稿写在 `TASK_ROOT/delivery/题面草稿.md`；
  > 控制台改为**自己创建 `drafts/<taskId>/题面草稿.md`**，安装时用 `prompt --file <draft 路径>`
  > 指过去（`--file` 接受任意路径）。不要去改技能生成的 `delivery/题面草稿.md`。
- **`staging/` 保留到任务归档**：方便「放弃本题」时重新解包，也便于核对 zip sha。
- **`logs/skill/`** 保存每次技能调用的完整 stdout/stderr，用于排查「命令跑了但结果不对」。
  注意 `run` 的**进度在 stderr**、容器输出在 `monitor/runs/attempt-NN/` 下的两个文件里
  （见 `13-skill-cli-contract.md` §1-F1）。

## 3. SQLite 表结构

启用 `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`

### 3.1 `tasks`

```sql
CREATE TABLE tasks (
  id              TEXT PRIMARY KEY,          -- 控制台内部ID，uuid4
  task_root       TEXT NOT NULL UNIQUE,      -- 绝对路径
  task_name       TEXT NOT NULL,
  source_kind     TEXT NOT NULL,             -- project_hub | local_dir
  engineering_id  TEXT,                      -- 题源侧ID
  project_id      TEXT,
  claim_id        TEXT,
  domain          TEXT NOT NULL,
  sub_direction   TEXT NOT NULL,
  task_type       TEXT,
  difficulty      TEXT,                      -- 困难 | 地狱
  languages       TEXT,                      -- 逗号分隔
  repro_level     TEXT,
  job_status      TEXT NOT NULL,             -- 控制台作业状态（见 §4）
  skill_status    TEXT,                      -- 取自 state.json status
  skill_version   TEXT,
  initial_sha     TEXT,
  artifact_sha    TEXT,
  session_id      TEXT,
  prompt_id       TEXT,
  prompt_sha256   TEXT,
  feedback_score  INTEGER,
  review_acked_at TEXT,                      -- 人工复核确认时间
  exported_at     TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  notes           TEXT
);
CREATE INDEX idx_tasks_status ON tasks(job_status);
CREATE INDEX idx_tasks_created ON tasks(created_at DESC);
CREATE UNIQUE INDEX idx_tasks_session ON tasks(session_id) WHERE session_id IS NOT NULL;
```

`idx_tasks_session` 是**唯一约束**，直接在库层面防止同一 SessionID 出现两次（防重复提交）。

### 3.2 `steps`

```sql
CREATE TABLE steps (
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  step_no     INTEGER NOT NULL,              -- 1..11
  state       TEXT NOT NULL,                 -- done | current | blocked | pending | failed
  detail      TEXT,                          -- 一句话原因或证据摘要
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (task_id, step_no)
);
```

11 步定义在 `pipeline/steps.py` 一处，控制台不硬编码序号。

### 3.3 `attempts`

```sql
CREATE TABLE attempts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,              -- attempt-01, 02...
  mode        TEXT NOT NULL,                 -- print | interactive
  container   TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  exit_code   INTEGER,
  outcome     TEXT NOT NULL,                 -- accepted | rejected | cancelled | timeout
  error_kind  TEXT,                          -- auth | structure | timeout | host_restart | unknown
  detail      TEXT,                          -- 原始错误摘要（脱敏）
  UNIQUE (task_id, seq)
);
```

### 3.4 `artifacts`

```sql
CREATE TABLE artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,                  -- trace | timeline | video | diff | verify | xlsx | bundle
  path       TEXT NOT NULL,
  size_bytes INTEGER,
  sha256     TEXT,
  meta       TEXT,                           -- JSON：时长、分辨率、文件数、增减行
  created_at TEXT NOT NULL
);
CREATE INDEX idx_artifacts_task ON artifacts(task_id, kind);
```

### 3.5 `events`（全局事件流）

```sql
CREATE TABLE events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  level      TEXT NOT NULL,                  -- info | warn | error
  actor      TEXT NOT NULL,                  -- user | scheduler | gateway | source | reconcile
  task_id    TEXT,
  action     TEXT NOT NULL,                  -- task.created / run.started / gate.failed ...
  message    TEXT NOT NULL,
  details    TEXT                            -- JSON
);
CREATE INDEX idx_events_at ON events(at DESC);
CREATE INDEX idx_events_task ON events(task_id, at DESC);
```

保留 30 天，超过按条删除（保留 error 级）。

### 3.6 `claims`（领取记录，用于归还与超时提醒）

```sql
CREATE TABLE claims (
  id              TEXT PRIMARY KEY,          -- 题源 claim_id
  project_id      TEXT NOT NULL,
  engineering_id  TEXT NOT NULL,
  engineering_name TEXT NOT NULL,
  claimed_at      TEXT NOT NULL,
  released_at     TEXT,
  task_id         TEXT REFERENCES tasks(id),
  zip_sha256      TEXT
);
```

## 4. 状态映射表（权威）

技能 `state.json.status` 与控制台 `job_status` 的组合判定：

| skill_status | 条件 | job_status | 展示 |
|---|---|---|---|
| `prepared` | — | `authored` | 「待出题」 |
| `prompt_ready` | 无 `remote.initialPushed` | `prompt_ok` | 「待推送快照」 |
| `prompt_ready` | 有 `remote.initialPushed` | `ready` | 「可运行」 |
| `running` | 监督器、容器或 tmux 任一存活 | `running` | 「运行中 · 第 2 轮 · 继续 1/2」 |
| `running` | `sessionRun.status=holding` | `blocked` | 「等待人工处理：鉴权」+ 倒计时 |
| `running` | `sessionRun.status=manual_input` | `blocked` | 「需要人工输入」 |
| `running` | 容器与 tmux 都不存在且接回失败 | `blocked` | 「会话丢失」 |
| `ran` / `snapshotted` | 录屏缺失 | `ran` | 「待录屏」 |
| `recorded` | feedback check 未过 | `recorded` | 「待写 Feedback」 |
| `recorded` | feedback ok 且未确认复核 | `recorded` | 「待复核确认」 |
| `recorded` | 已确认 | `reviewed` | 「待导出」 |
| `exported` | — | `exported` | 「已导出」 |
| `session_aborted` | `continues_exhausted` | `aborted` | 「继续用尽，需换题」 |
| `session_aborted` | 其他终止原因 | `aborted` | 终止原因原文；工程冷却 |
| `run_failed` | error_kind=auth | `blocked` | 「Key 失效，全队暂停」 |
| `run_failed` | print 回退的结构失败 | `aborted` | 「一次机会已用，换题」 |
| 任意 | 人工放弃 | `aborted` | 「已放弃」 |

控制台的 `queued` 只在内存队列里存在，不写库；重启后按上表重算，不会「卡在排队」。

## 5. 对账（reconcile）

每 60 秒执行一次，也可手动触发 `POST /api/system/reconcile`：

```
for task in tasks where job_status != 'archived':
    state = read_json(task_root/monitor/state.json)
    if state missing → 标记 orphan，事件 warn
    if state.schema != 'solo-2611/1' → 标记 incompatible，只读
    重算 job_status（§4 表）
    重算 11 步，写 steps 表
    更新 tasks 表关键字段（session_id、sha、score）
冲突时：以磁盘为准，写事件 reconcile.corrected，附旧值与新值。
```

**不做反向写入**：对账只修正索引，绝不改技能目录里的任何文件。

## 6. 数据保留与清理

| 数据 | 保留 | 清理方式 |
|---|---|---|
| `traces/*.jsonl` | 永久（交付证据） | 手动 |
| `recording/*.mp4` | 永久（>10MB 需传对象存储） | 手动 |
| `workspace/` | 导出后可删（保留 SHA 与 diff） | `POST /api/tasks/{id}/prune-workspace` |
| `monitor/runs/*/traces` | 导出后 7 天 | 自动 |
| `staging/*` | 归档后 3 天 | 自动 |
| `events` | 30 天 | 自动 |
| `logs/skill/*` | 14 天 | 自动 |

清理必须有二次确认，且默认 `dry-run` 预览将删除的路径与体积。

## 7. 备份

`POST /api/system/backup` 打包 `solo-auto.db + config.toml + drafts/`（**不含** traces/视频，
体积太大且已有 GitHub 快照）。产出 `backup-YYYYMMDD-HHMM.zip`，默认存 `~/solo-auto/backups/`。

## 8. 迁移

首版一个 `schema_version` 表 + 顺序 SQL 文件。升级时：

```sql
INSERT INTO schema_version(version, applied_at) VALUES (2, ...);
```

已知需要的第一次迁移：无（v1 建全表）。后续技能字段变化通过「兼容模式」处理（见 `02` §7），
不轻易改表结构。
