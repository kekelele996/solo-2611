# 02 · 架构与数据流转

## 1. 分层原则

沿用上一期被验证过的分工：**技能是执行者，控制台是调度与监看，两者只靠文件契约耦合。**

```
┌─────────────────────────────────────────────────────────────────┐
│  浏览器（127.0.0.1:8099）                                        │
│  React 19 + Vite + TanStack Query                               │
└───────────────────────────────┬─────────────────────────────────┘
                                │ HTTP / SSE
┌───────────────────────────────▼─────────────────────────────────┐
│  控制台后端（FastAPI, 127.0.0.1:8099）                           │
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────┐  │
│  │ 题源适配 │ │ 作业调度 │ │ 门禁引擎 │ │ 证据归集 │ │ 导出  │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └───┬───┘  │
│       │            │            │            │           │      │
│       │      ┌─────▼────────────▼────────────▼───────────▼───┐  │
│       │      │   SkillGateway：唯一允许调用技能的地方        │  │
│       │      └─────────────────────┬───────────────────────┘  │
│       │                            │                            │
│  ┌────▼─────┐              ┌───────▼────────┐                   │
│  │ SQLite   │              │ 磁盘：TASK_ROOT │                   │
│  │ 索引     │              │ 唯一事实源      │                   │
│  └──────────┘              └───────┬────────┘                   │
└────────────────────────────────────┼────────────────────────────┘
                                     │ subprocess
                    ┌────────────────▼─────────────────┐
                    │ solo2611.py（技能，只读调用）     │
                    │  init/prompt/repo/run/record/    │
                    │  feedback/export/status/bundle   │
                    └────────────────┬─────────────────┘
                                     │ docker run / ScreenCaptureKit
                    ┌────────────────▼─────────────────┐
                    │ 官方镜像容器 / 本机录屏器         │
                    └──────────────────────────────────┘
                                     │ HTTP
                    ┌────────────────▼─────────────────┐
                    │ 工程台 Project Hub（可配置地址）  │
                    └──────────────────────────────────┘
```

### 三条硬规则

1. **只有 `SkillGateway` 可以启动技能子进程。** 其他模块要数据就读磁盘或查索引，绝不再起一遍进程。
2. **磁盘是事实源，SQLite 是缓存。** 任何冲突以 `state.json` 为准，索引自动修正并记事件。
3. **控制台不改写技能产物。** `delivery/提示词.md`、`feedback.md`、`交付表.xlsx` 一律由技能写；
   控制台的编辑功能保存为「草稿文件」（如 `drafts/题面草稿.md`），再交给技能的门禁去判定。

## 2. 组件清单

| 组件 | 职责 | 只读/可写 |
|---|---|---|
| `sources/base.py` | 题源协议：`list_projects` / `list_engineering` / `claim` / `release` / `download_zip` / `detail` | 远程写（领取/归还） |
| `sources/project_hub.py` | 工程台实现：Cookie 登录 + CSRF、Bearer 令牌、分页、ZIP 流式下载 | 同上 |
| `sources/local_dir.py` | 本地目录实现（离线/试标用，不领取） | 本地读 |
| `gateway/skill.py` | 技能定位、版本探测、命令白名单、参数拼装、超时、输出捕获 | — |
| `pipeline/steps.py` | 11 步定义与判定函数（见 §4） | — |
| `pipeline/scheduler.py` | 队列、容器槽位、运行中互斥、退避重试 | — |
| `pipeline/runner.py` | 单任务执行器（异步，可中断） | — |
| `gates/*.py` | 题面门禁、Feedback 门禁、录屏门禁、导出完整性 | — |
| `evidence/*.py` | 轨迹解析（复用技能 `trace.py` 的输出）、diff、时长、结构校验 | — |
| `store/repo.py` | SQLite CRUD 与对账 | 写 |
| `store/state_watch.py` | 文件监视（watchdog），`state.json` 变化即刷新 | 读 |
| `api/*.py` | HTTP 路由 | — |
| `events/bus.py` | 事件总线 + SSE 广播 | — |
| `secrets.py` | Keychain / 0600 文件封装 | — |

## 3. 数据流转：一次完整的题目生命周期

### 阶段 0 · 选题（人主导）

```
用户选择项目  ──►  GET /open/v1/projects/{id}/engineering?scope=available&page=1
                    ↓
             展示工程列表（名称/语言/文件数/标签/截图）
                    ↓
             用户点「领取」 ──► 确认弹窗（独占、会超时回收）
                    ↓
             POST /api/v1/projects/{id}/engineering/{eid}/claim
                    ↓
             GET  .../zip  ──► 流式下载到 ~/solo-auto/staging/<eid>.zip
                    ↓
             校验 sha256 == engineering.zip_sha256
                    ↓
             解包到 staging/<eid>-<ts>/，落盘 meta.json（engineering_id、version、claim_id、tags）
```

控制台此时只记录一条 `pending_task`，还没有 `TASK_ROOT`。这一步**不碰技能**。

### 阶段 1 · 建任务（人触发，程序执行）

```
POST /api/tasks { engineering_id, draft_name }
  ↓ 读 staging meta
  ↓ 技能 init --task-name NAME --source <staging 目录> [--tasks-dir <配置>]
  ↓ 技能输出 {"taskRoot": ..., "initialSha": ...}
  ↓ 建 SQLite task 行 + 索引目录
  ↓ 事件 task.created
```

### 阶段 2 · 出题（人或外部 Agent 主导，程序做门禁）

```
编辑 drafts/题面草稿.md     ← 人在控制台里写，或外部 Agent 直接改文件
编辑 monitor/prompt/difficulty-review.json
编辑 monitor/prompt/ra-renhua-review.json
  ↓
点「安装题面」 → 技能 prompt --file ... --task-type ... --difficulty ... --lang ...
  ↓  返回 ok=false 时把 errors 原文展示，按钮保持禁用
  ↓  返回 ok=true 时写 delivery/提示词.md，state.status = prompt_ready
```

### 阶段 3 · 推送快照（人触发）

```
技能 repo --task-root ROOT
  ↓ stat.remote.initialPushed 写入
  ↓ 展示 https://github.com/<owner>/<repo>/commit/<sha>
```

### 阶段 4 · 运行（人触发，程序异步执行 + 排队）

```
点「运行」→ scheduler.enqueue(task)
  ↓ 队列闸门 = 技能 config 的 claude.maxContainers（同一个数，见 13 §1-F2）
  ↓ SkillGateway.run(["run", "--task-root", ROOT])  Popen
  ↓ stderr 逐行 → 阶段（[attempt N] 容器…运行中 / 不可用：…）
  ↓ monitor/runs/attempt-NN/stdout.jsonl tail → 容器内的动作
  ↓ 结束：+ 忽略 stdout（只有最后一次 emit，退出码 0/2 时解析）
  ↓ 技能已自动做 snapshot/digest/auto-push
  ↓ 读 state.json：session / artifact 是否齐全
  ↓ 齐全 → status=ran；不齐全 → status=run_failed，按错误类型给建议
```

> 详细契约见 `13-skill-cli-contract.md`：`run` 的进度在 **stderr**，容器输出在两个文件，
> 技能的 stdout 只在结束时输出一次 JSON。只读 stdout 会表现为「长时间无输出后突然结束」。

### 阶段 5 · 证据阅读（人/Agent 主导）

```
GET /api/tasks/{id}/timeline   → 读 delivery/轨迹时间线.md
GET /api/tasks/{id}/diff       → git diff（只读，调用 git 命令）
GET /api/tasks/{id}/verify     → 读 monitor/runtime-verification.json（若存在）
POST /api/tasks/{id}/verify   → 在产物目录执行用户填写的命令，落盘输出
```

### 阶段 6 · 录屏（人触发，程序协作）

```
选窗口 → record list  → 用户点选 windowId
  ↓ record start --task-root ROOT --window-id N --plan ...
  ↓ 前端倒计时 3-2-1 → 用户操作产物
  ↓ record stop
  ↓ 读 运行录屏-window-capture.json / cursor-guard.json
  ↓ 校验 1280x720、≤90s、captureKind=window-id
```

### 阶段 7 · Feedback（人或 Agent 主导，程序门禁）

```
feedback --init  → 生成模板
编辑 delivery/feedback.md
  ↓ 保存时 feedback --check
  ↓ ok=false 展示 errors；ok=true 展示仪表盘
  ↓ 勾选「我已逐条复核事实与措辞」→ 写 monitor/review-ack.json（含时间、字数、score）
```

### 阶段 8 · 导出（人触发）

```
export 或 export-batch
  ↓ 读 11 项完成度
  ↓ 生成 xlsx → openpyxl 读回校验表头
  ↓ 落 SQLite export 行，事件 task.exported
```

### 阶段 9 · 归集（可选）

```
技能 bundle → 产物材料.md（题面 + 时间线 + 全量 diff）
技能 cleanup → 删容器
工程台 release → 归还工程
```

## 4. 状态机

### 4.1 任务状态（控制台侧，与技能 `state.status` 组合）

技能自身的 `status` 值域：`prepared → prompt_ready → running → ran → snapshotted → recorded → exported`，
异常时 `run_failed`。控制台不覆盖这些值，而是维护一个**平行的作业状态**用于排队与展示：

```
                    ┌──────────┐
                    │ staged   │  ZIP 已下载校验，未建任务
                    └────┬─────┘
                         │ init
                    ┌────▼─────┐
              ┌─────┤ authored │  任务已建，题面未安装
              │     └────┬─────┘
              │          │ prompt ok
              │     ┌────▼─────┐
              │     ├ prompt_ok │  题面已装，快照未推
              │     └────┬─────┘
              │          │ repo ok
              │     ┌────▼─────┐
              │     ├ ready    │  可运行
              │     └────┬─────┘
              │          │ enqueue
              │     ┌────▼─────┐   ┌────────────┐
              │     ├ queued   ├──►│ blocked    │ 槽位满 / 403 / Key 失效
              │     └────┬─────┘   └─────┬──────┘
              │          │ start           │ 恢复
              │     ┌────▼─────┐◄──────────┘
              │     ├ running  │
              │     └────┬─────┘
              │          │ 结构通过
              │     ┌────▼─────┐   结构不通过
              │     ├ ran      ├──────►┐
              │     └────┬─────┘       │
              │          │ record      │ retry<3
              │     ┌────▼─────┐◄──────┘
              │     ├ recorded │
              │     └────┬─────┘
              │          │ feedback check ok + 复核
              │     ┌────▼─────┐
              │     ├ reviewed │
              │     └────┬─────┘
              │          │ export
              │     ┌────▼─────┐
              │     ├ exported │
              │     └────┬─────┘
              │          │ 归还 + cleanup
              │     ┌────▼─────┐
              └────►│ archived │
                    └──────────┘

任一状态 ──► aborted（人工放弃，需填原因，归还前必须 cleanup）
```

**为什么要有平行的作业状态**：技能的 `status` 不记录「排队中」「被阻塞」「已排队但槽位未到」，
也不区分「ran 但录屏未做」。两条状态并存时，展示层以「技能状态 + 控制台阶段」联合判定，
在 `04-data-storage.md` 给出权威映射表。

### 4.2 11 步判定（`pipeline/steps.py`）

| # | 步骤 | 技能状态/产物 | 判定函数 |
|---|---|---|---|
| 1 | 初始快照 | `state.initialSha` 是 40 位 SHA | `is_full_sha` |
| 2 | 远端快照 | `state.remote.initialPushed` 存在 | — |
| 3 | 题面 | `promptPath` 存在且 sha 未变 + `prompt/validation.json.ok` | 文件哈希比对 |
| 4 | 单轮会话 | `state.session.sessionId` | — |
| 5 | PromptID | `state.session.promptId` | — |
| 6 | 产物快照 | `state.artifact.sha` ≠ initial | — |
| 7 | 远端产物 | `state.remote.artifactSha` | — |
| 8 | 轨迹时间线 | `delivery/轨迹时间线.md` 存在 | — |
| 9 | 运行录屏 | 视频存在 + capture 报告 ok + cursor guard ok + 时长/分辨率 | 复用技能 `status` 的判定逻辑 |
| 10 | Feedback | `feedback-check.json.ok` + 复核确认 | + 控制台 `review-ack.json` |
| 11 | Excel | `state.export.missing == []` | — |

与技能 `status` 子命令保持同一口径，但控制台额外要求 #10 的人工复核确认，防止「机器过了就交」。

## 5. 并发模型

### 5.1 三类资源，三种锁

| 资源 | 并发度 | 实现 |
|---|---|---|
| 容器槽位 | 默认 4，可配 1–8 | **互斥由技能实现**（`~/.codex/solo-2611/containers.lock` + `docker ps --filter label=solo2611=true`）。控制台只读 `config.claude.maxContainers` 作为**自己队列的闸门**，并在 `run/slots/` 写展示用的占位文件 |
| 录屏 | 1（主机级、跨任务） | **技能已实现**（`recorder._acquire_recording_slot`）。控制台只把「另一个任务正在录屏」的报错翻译成提示 |
| 工程领取 | 每工程 1（服务端保证） | 控制台本地再记 `claim_id`，防止重复领取自己的 |

**为什么把锁让给技能**：技能内部已经有锁，且它在拿不到名额时是**静默阻塞等待**而不是报错。
如果控制台用更大的上限去排队，会出现「界面显示 4 个运行中 + N 个等待，实际有 N 个进程卡在技能里等名额」——
状态就开始撒谎了。两边用同一个数（读同一个 config），技能的等待分支永远不触发。

因此控制台的槽位目录**只用于 UI 展示与僵尸回收**，不承担互斥职责。
互斥的唯一事实来源是 `docker ps --filter label=solo2611=true` 的实时结果。

### 5.2 队列调度算法

```
每 2 秒 tick：
  1. 反查存活：docker ps --filter label=solo2611=true --format '{{.Names}}'
     → 刷新 slots/ 展示文件，进程已死的条目删除并记事件
  2. 计算 used = 存活容器数（不是「记录里说 running 的任务数」）
  3. limit = 技能 config 的 claude.maxContainers
     若 used >= limit → 返回
  4. 从 queued 里按优先级取：
       优先级 = (人工置顶 ? 0 : 1, 入队时间)
  5. 前置校验：state.status 必须是 prompt_ready/run_failed；
     workspace HEAD == initialSha 且干净（不干净先提示 reset）
  6. 启动，写展示用 slot 文件
  7. 同一 tick 最多启动 1 个（避免同时拉镜像打爆网络）
```

### 5.3 为什么不做「按任务数」的第二种模式

上一期有两套容量模型（containers / tasks），因为一道 GSB 题要两个容器、收尾阶段容器释放但任务未结束。
本期一题一容器、运行结束即释放，两者近似等价，只保留 containers 模式。少一个概念，少一类 bug。

## 6. 断点恢复

| 场景 | 恢复策略 |
|---|---|
| 控制台进程被杀 | 重启后读 SQLite + 扫描磁盘，按 `state.json` 重建；运行中的任务标记为 `unknown`，由 `docker ps` 反查容器名是否存活 |
| 电脑重启 / Docker 重启 | 容器已失；运行中任务转为 `run_failed(reason=host_restart)`，提示 `reset --yes` 后重跑 |
| 技能中途崩溃 | `state.status=running` 但无进程 → 标记 `stale`，提供「重置并重跑」 |
| 录屏进程残留 | 启动时读技能写的 `~/.codex/solo-2611/recording-lock.json`，`ownerPid` 不存活则提示「上次录制异常退出，可安全重录」（由技能在下次 start 时自愈，控制台不删技能文件） |
| SQLite 损坏 | 删除索引文件，全量重建（只读磁盘，无数据损失） |
| 磁盘满 | 启动时检查剩余空间 < 30GB → 只读模式，禁止新建任务 |

**恢复的判定依据是「进程是否存活 + 磁盘状态」，而不是「记录里说它 running」。**
这一点是上一期踩过的坑：技能只看状态不判进程，导致队列认为任务还在跑。

## 7. 与技能的耦合契约

控制台依赖技能的以下输出，全部视为**外部契约**，任一缺失即降级而不是崩溃：

| 契约 | 用途 | 缺失时 |
|---|---|---|
| `state.json` 的 `schema` = `solo-2611/1` | 判断可否解析 | 版本不匹配 → 只读展示原始 JSON |
| `state.json` 的 `status` 值域 | 阶段判定 | 未知值 → 显示原值 + 黄色警告 |
| `prompt/validation.json` 的 `ok` | 题面门禁 | 缺失 → 步骤 3 判为未通过 |
| `feedback-check.json` 的 `ok/score/warnings` | Feedback 门禁 | 缺失 → 步骤 10 未通过 |
| 命令的 stdout JSON | 参数回传（taskRoot、sha、errors） | 解析失败 → 保存原文，展示「命令已执行但输出无法解析」 |
| 退出码 | 成功/失败 | 非 0 一律视为失败，附 stderr |

**版本探测**：启动时执行 `solo2611.py version` 与读取 `VERSION.json`，把 `skillVersion` 写入索引。
技能升级导致字段变化时，控制台走「兼容模式」：缺失字段显示 `—` 并横幅提示「技能已升级到 x.y.z，请核对新版字段」。

## 8. 安全边界

- HTTP 服务只绑 `127.0.0.1`，不做 CORS 通配，不做远程访问开关。
- 工程台凭据支持两种：Cookie（用户名/密码，登录后存 `csrf` 与 session cookie）与 Bearer 令牌（只读）。
  因为**领取是写操作**，必须用 Cookie 会话或管理员令牌；控制台默认要求 Cookie 会话。
- 密码/令牌存 macOS Keychain（`service=solo-auto`，`account=projecthub`），查看需二次确认。
- 所有子进程显式传 `env`，不继承 `ANTHROPIC_API_KEY` 等敏感变量之外的配置；命令回显前做脱敏。
- 页面上的「复制命令」输出已脱敏版本（Key 用 `<from-keychain>` 占位）。
