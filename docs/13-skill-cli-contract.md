# 13 · 技能 CLI 契约（已核对源码）

> 本文档已按 v0.6.0 受管交互实现更新；旧版 print 行为只保留在回退说明中，**不是**从
> `SKILL.md` 推演。凡与 `SKILL.md` 措辞不同处，以本文档为准。
>
> 开发时必须按本文档实现网关，不要凭命令名猜参数 —— 猜错会导致「命令跑了但结果不对」，
> 这是最难排查的一类问题。

## 1. 三个必须知道的实现事实

这三条直接改变控制台的设计，写在最前面。

### F1 · 受管 `run` 返回启动凭据，后续进度来自 `run-status` 与事件日志

即：

| 流 | 内容 | 控制台怎么用 |
|---|---|---|
| 首次 `run` stdout | 启动 JSON，含 attempt/container/tmux/supervisor | 立即解析，之后不再保持长连接 |
| `run-status --json` | 状态、轮次、继续、截止时间、最近事件 | 每 2–5 秒轮询，写工作台影子状态 |
| `supervisor.log` | 一行一个事件，只增不改 | 实时日志、轮次时间线、SSE 事件来源 |
| `monitor/runs/attempt-NN/live/` | 实时 Claude projects 目录 | 监督器的唯一语义判定来源 |

**所以**：工作台不 `await` 一个可能运行数小时的子进程。后端重启只重新轮询状态；调用方脚本退出、
Agent 超时或 SSH 断开都不影响监督器和容器。

### F2 · 技能自己已经实现了容器上限，会**静默阻塞**

```python
with file_lock(CONTAINER_LOCK):            # ~/.codex/solo-2611/containers.lock
    while len(_running_solo_containers()) >= limit:
        print(f"容器已达上限 {limit}，等待名额…", file=sys.stderr)
        time.sleep(10)
```

- `limit` 取自 `config.json` 的 `claude.maxContainers`（默认 4），**`run` 没有 `--limit` 参数**。
- 超过上限时它不是报错，而是**卡在那里等**，而且已经先把 `state.status = "running"` 写进磁盘。

后果：如果控制台自己的队列上限大于 `maxContainers`，界面会显示 4 个「运行中」+ N 个「运行中（其实在等名额）」。

**结论**：控制台不设独立的并发上限，而是**读取 `claude.maxContainers` 作为自己队列的闸门**。
两边用同一个数，技能的等待分支永远不触发，控制台的状态也就不会撒谎。
（`04`/`02` 里「控制台维护槽位目录」的复杂度可以砍掉大半：槽位只用于 UI 展示，
真正的互斥交给技能；控制台只需 `docker ps --filter label=solo2611=true` 反查存活。）

### F3 · 录屏锁是**主机级、跨任务**的，技能已实现

```python
def _acquire_recording_slot(task_root):
    if owner_pid and _alive(owner_pid) and current.get("taskRoot") != str(task_root):
        raise Solo2611Error(f"另一个任务正在录屏：{current.get('taskRoot')}（pid {owner_pid}）。"
                            "所有任务共用主机级录屏锁，等它结束后再开始。")
```

- 同一任务重复 start 也报错：`已有录屏在进行（pid …），先 record stop`。
- 所以 `FR-REC-08`（全局串行）**不需要控制台实现**，只需把错误原文展示出来。
- 控制台唯一要做的是把这个错误翻译成「gb-xxx 正在录屏，请等待」，而不是「操作失败」。

## 2. 命令总表（参数已核对）

所有命令都可用 `python3 solo2611.py <cmd> --help` 自证。

### 2.1 `version`

```
$ python3 solo2611.py version
0.2.2
```

纯文本，不是 JSON。控制台应同时读 `VERSION.json` 取 `period` / `image` / `harnessVersion`。

### 2.2 `config`

```
config <init|show|set|path|key|test> [pairs...] [--force]
```

- `config show` → JSON，**密钥已脱敏为 `sk-s***Wg` 形式**（可直接展示给用户）。
- `config key` → 打印明文 API Key（供 `interactive` 的启动命令用；控制台**绝不调用并展示**）。
- `config path` → 配置文件路径。
- `config set key=value` → 点号路径赋值。

### 2.3 `init`

```
init --task-name NAME [--source DIR | --git URL [--ref REF]] [--tasks-dir DIR]
```

- 输出（stdout，JSON 一次）：
  ```json
  {"taskRoot":"…","workspace":"…","initialSha":"<40位>","next":"prompt"}
  ```
- 任务目录名：`{YYYY-MM-DD}-{safe_slug(task_name)}-{4位随机}`。
- **路径不能含逗号**（`docker --mount` 参数会被拆开），技能会直接报错。
- 若 `--source` 指向的目录本身是 git 仓库：走 `git clone --no-hardlinks`，
  且**未提交改动不会带过去**（会打 warning 到 stderr）。
- 若 `--source` 不是 git 仓库：`copytree` + `git init -b main` + 一个初始提交。
- `--source` 与 `--git` 都不给 → 报错 `需要 --source DIR 或 --git URL`。
- 会自动：
  - 追加 `.gitignore` 条目（凭据与产物）；
  - 检查是否跟踪了 `.env*` / `*.pem` / `*.key` 等，**命中即失败**（不会静默提交）；
  - 写入 `monitor/state.json`（`status: prepared`，`fields` 取 config 的 `defaults.*`）。
- `state.fields` 里只有 4 项：`所属领域`、`子方向`、`Harness`、`操作系统`。
  其余 5 个业务字段要等 `prompt` 成功后才写入。

### 2.4 `prompt`

```
prompt --task-root ROOT [--init [--force]]
prompt --task-root ROOT --file PATH --task-type T --difficulty D --lang L
       [--domain X] [--sub-direction Y] [--repro R]
       [--review PATH] [--difficulty-review PATH]
       [--allow-over-170] [--force]
```

- `--init`：**草稿写在 `TASK_ROOT/delivery/题面草稿.md`**（不是 drafts 目录），
  同时铺 `monitor/prompt/difficulty-review.json` 与 `ra-renhua-review.json` 模板（不覆盖已填内容）。
  输出 `{"ok":true,"draft":…,"templates":{…},"next":"…"}`。
- `--file` 接受任意路径 → **控制台可以把草稿放在自己的 `drafts/<id>/题面草稿.md`**，
  安装时用 `--file` 指过去，技能会写到 `delivery/提示词.md`。这就是「草稿分离」的落地方式。
- `--task-type` / `--difficulty` / `--lang` **三者必填**，漏了会明确报错。
- 已跑过（`state.attempts` 非空）且不带 `--force` → 报错「提示词不能再改」。
  **控制台不暴露 `--force`**，改题走「重开本题」并留原因（FR-PRM-06）。
- 失败路径：`emit({"ok": false, "errors": [...], "warnings": [...]})` 然后 **`raise SystemExit(2)`**。
  → 退出码 **2**，stdout 有结构化 JSON。这是与普通失败（退出码 1）不同的信号，必须分别处理。
- 成功：`emit({"ok":true,"promptPath":…,"sha256":…,"warnings":[…],"difficultySignalsPassed":…,"next":"repo"})`，
  并写入 `monitor/prompt/validation.json`、把 `status` 改为 `prompt_ready`。
- **相似题检测已内置**：读 `~/.codex/solo-2611/prompt-history.jsonl`，
  12 字符 shingle 有交集 → error；`difflib` 相似度 ≥0.75 → error；≥0.55 → warning。
  **控制台不要自建相似度检测**（FR-PRM-07 改为「展示技能返回的 warnings」）。

### 2.5 `repo`

```
repo --task-root ROOT [--name PREFIX] [--private]
```

- **默认创建公开仓库**（`--private` 才私有），帮助文本写明「规范要求评测团队可访问，默认公开」。
  → 见 §6 的勘误：不要在控制台里擅自加 `--private`。
- 仓库名 = `safe_slug(name or taskName)` + 4 位随机后缀。
- 可重复执行，只补推缺的快照。
- 刚建的空仓库如果推不上去，技能会**自动删除该远程仓库**再抛错（避免留下空公开仓库）。
- 输出：
  ```json
  {"repo":"https://github.com/<o>/<r>","private":false,
   "初始环境快照":"https://github.com/…/commit/<sha>","产物快照":"（运行后自动推送）","verified":true}
  ```
- Token 取自 `github.accessTokens`，通过 `http.https://github.com/.extraheader` 注入，
  **不写进 remote URL**（`git remote -v` 看不到 token）。

### 2.6 `run`

```
run --task-root ROOT [--mode supervised|print] [--follow]
    [--timeout SEC] [--skip-repo] [--hard-stop]
```

v0.6.0 默认 `claude.runMode=supervised`。受管模式调用官方镜像的 `interactive` 入口，
在后台 tmux 中运行，监督器独立于调用方进程。`run` 只负责创建 attempt、启动 tmux/容器和监督器，
默认立即返回：

```json
{"ok":true,"supervised":true,"attempt":1,"container":"…","tmux":"…","supervisor":12345,"next":"run-status"}
```

- 一题只有一次会话机会；不存在“恢复初始快照后重跑”。
- 会话成立后，429、5xx、截断、空回复、许诺下一步或向用户提问都在原会话发送字面 `继续`，最多 2 次。
- 401、额度不足、模型名不匹配原地保留，最多等待处理 30 分钟；超时终止。
- 总时长默认 10800 秒；无进展/静默默认 1200 秒；到达任一上限即终止。
- 监督器、Agent、工作台后端断开不算会话中断；`resume` 接回不消耗继续次数。
- `--follow` 持续读取状态直到终态。`--mode print` 是保守回退，仍然只有一次机会、无继续。
- `--hard-stop` 只改变 Ctrl-C 行为：立刻停容器并恢复工作区；默认 Ctrl-C 是脱离，不终止会话。

对应查询与控制命令：

```
run-status --task-root ROOT [--json] [--lines N]
resume --task-root ROOT [--follow]
continue --task-root ROOT [--note TEXT]
abort --task-root ROOT --reason TEXT [--by human|auto]
resume-all [--tasks-dir DIR] [--dry-run]
doctor baseline [--tasks-dir DIR] [--out FILE]
doctor --fix-stale [--tasks-dir DIR]
supervise --task-root ROOT --attempt N [--attach]
```

- `run-status --json` 是工作台的唯一轮询入口，返回状态、轮次、继续 x/2、最近活动、下次动作、
  剩余时间、监督器/容器存活与事件尾。
- `resume` 幂等；监督器活着时只查询，不重复启动监督器。
- `continue --note` 中的人类备注只写事件日志，发给模型的文本严格只有两个字 `继续`。
- `abort` 是唯一会停止并删除受管会话容器的入口；终止后写 `delivery/终止说明.md` 并保留现场。
- `start_failed` 不消耗会话机会，最多重新启动 2 次；仍失败则任务可重新排队，不计工程冷却。

`run` 的全局前置校验与旧版相同：题面未改、初始快照已推送、workspace 干净、尚未接受或终止。
会话接受后仍自动执行 `_snapshot → _digest → _auto_push`，控制台不需要再重复手工快照。

### 2.7 `interactive` / `collect`

```
interactive --task-root ROOT [--skip-repo]
collect --task-root ROOT [--container NAME]
```

- `interactive` 不返回 JSON，而是**打印一段给人看的文本**：启动命令 + 提示词已复制到剪贴板。
  它同时把容器名写进 `state.pendingContainer`。
- 控制台此时不应解析这段文本，而是直接把它显示在代码块里（甚至原样复制到剪贴板）。
- `collect` 在容器退出后执行，行为与 `run` 的接受逻辑相同（`emit` 成功/失败 JSON）。
- `cleanup` 遇到未 collect 的交互容器会拒绝，需要 `--force`。

### 2.8 `snapshot` / `digest` / `status`

```
snapshot --task-root ROOT    # emit(state["artifact"])
digest   --task-root ROOT    # print 一段 Markdown 文本（不是 JSON）
status   --task-root ROOT    # print 人读的 11 行清单（不是 JSON）
```

- `digest` 与 `status` **输出的是人读文本**，控制台要么原样展示，要么自己按同一口径判定（推荐后者，见 `02` §4.2）。
- `status` 的 11 项名称与顺序（**这就是 UI 左栏 StepRail 的文案来源**）：

  ```
  初始快照 / GitHub 初始快照 / 提示词（难度+人话） / 单轮会话 / TurnID/PromptID /
  产物快照 / GitHub 产物快照 / 轨迹时间线 / 运行录屏 / Feedback（证据化并复核） / Excel
  ```

- `status` 的录屏判定包含 7 个条件：`video` 非空、`windowCaptureOk`、capture 报告 `ok`、
  cursor 报告 `ok`、`pointerStrategy == "none"`、以及 **web 模式时的 6 项 Chrome 独立实例校验**
  （`dedicatedInstance`、`reusedRunningChrome == false`、`userChromeTouched == false`、
  `debugPortOwnerVerified`、`profileCleanup.status == "removed"`、`existsAfter == false`）。
  控制台自行判定时必须复刻这 7 条，否则会出现「控制台说绿了、导出时缺录屏」。

### 2.9 `record`

```
record list                                           # 不需要 --task-root
record plan --task-root ROOT --mode terminal|web [--force]
record check-plan --file PATH
record start --task-root ROOT --window-id N [--max 90] [--plan PATH]
record stop --task-root ROOT
record for  --task-root ROOT --window-id N [--max 90] [--seconds 60]
record import --task-root ROOT --file PATH            # 其它系统/手动导入
record chrome-start --task-root ROOT --url URL
record chrome-stop  --task-root ROOT
```

- `list` 输出**表格文本**，不是 JSON：

  ```
      123    Terminal              1920x1080  zsh — ~/work
  ```

  控制台必须按 `{windowId:>7}\s{app:<22}\s{width}x{height:<6}\s{title}` 解析。
  > 这是整个 CLI 里唯一的「只能正则解析」的地方，建议封装成 `parse_window_list()` 并配单测。

- `start` 立即返回（打印 `{"recording":true,"window":{…},"hint":"…"}`），录制在后台跑；
  `stop` 才落盘并更新 `state.recording`。
- **`--max` 上限就是 90**，传超过 90 会被 `recorder.start` 拒绝（`录制最长 90 秒；规范不允许剪掉失败片段`）。
- `--plan` 指定的计划必须存在且 `targetApps` 包含所选窗口的应用，否则报错。
- `start` 前需要 Swift 录制器就绪：首次会 `swiftc` 编译（约 5–20 秒），
  控制台的「开始录制」按钮应有短 loading 而不是秒回。
- `import` 会把 `captureKind` 记成 `manual-import`，`status` 的录屏判定会在
  capture 报告与 cursor 报告**都为 `manual-import`** 时才放行 —— 即它是合法的兜底路径，
  但规范要求的窗口级采集仍然是正路。控制台按 FR-REC-07 不主动提供入口。

### 2.10 `feedback`

```
feedback --task-root ROOT --init      # 模板写入 delivery/feedback.md（已存在则不覆盖，print 路径）
feedback --task-root ROOT --check     # 检查并写 monitor/feedback-check.json
```

- `--check` 输出 `{"ok","score","errors","warnings"}`，**不通过时退出码 2**。
- 同时把结果写进 `state.feedbackCheck = {ok, score, warnings, sha256, checkedAt}`。
- `--init` 输出的是纯文本路径，不是 JSON。

### 2.11 `export` / `export-batch`

```
export --task-root ROOT [--allow-incomplete]
export-batch [--tasks-dir DIR] [--date YYYY-MM-DD] [--out PATH]
```

- `export` 有缺失项且不带 `--allow-incomplete` → `emit({"ok":false,"missing":[…]})` + 退出码 2。
- 带 `--allow-incomplete` 时会**照样生成 xlsx**，但 `status` 不改为 `exported`，
  `state.export.missing` 里留着缺失清单。
  > **控制台绝不使用 `--allow-incomplete`**（FR-EXP-03）。它存在是给人工兜底排查用的。
- `export-batch` 遍历 `tasks_dir/*/monitor/state.json`，**按 SessionID 与 `promptSha256` 双重去重**，
  重复项进 `skipped`。
- 输出 `{"ok","xlsx","rows","skipped":[{"task","missing":[…]}]}`。
- 生成的 xlsx 固定两个 sheet：`提交表`（19 列 + 分组表头）与 `内部记录`。
  写入后会 `read_back` 校验表头，校验失败即抛错。

### 2.12 `bundle` / `cleanup`

```
bundle  --task-root ROOT [--out PATH]
cleanup --task-root ROOT [--force]
```

- `bundle` 输出 `{"path","sizeKB","files","diffSkipped"}`，默认写到源目录（`--source` 的那一层）
  或 `TASK_ROOT/delivery/产物材料.md`。跳过 lock 文件与 `.min.js` / `.map`。
- `cleanup` 输出 `{"removed":[容器名…]}`，按 `label=solo2611.task=<hash>` 匹配。

## 3. 退出码语义（必须区分）

| 码 | 含义 | 典型命令 | 控制台处理 |
|---|---|---|---|
| 0 | 成功 | 全部 | 解析 stdout JSON |
| 1 | 业务失败（`Solo2611Error`） | 前置校验、网络、404 | 展示 `❌ ` 后的 stderr 原文 |
| 2 | **门禁未通过**（结构化） | `prompt`、`export`、`feedback --check`、`collect` | 解析 stdout 的 `errors`/`missing` 并逐条展示 |
| 非 0 且无 stdout | 崩溃/未捕获异常 | 任意 | 展示 stderr 尾部 + 完整日志 |

`1` 与 `2` 的区分很重要：`2` 意味着「参数没问题，是内容不合规」，界面应高亮具体条目；
`1` 意味着「环境或前置条件有问题」，界面应给出修复动作（去配置、去 repo、去 reset）。

## 4. `state.json` 真实结构（v0.2.2 实测）

```jsonc
{
  "schema": "solo-2611/1",
  "skillVersion": "0.2.2",
  "taskName": "gb-544-late-event-versioning",
  "taskRoot": "/Users/…/2026-11-10-gb-544-…-un2p",
  "workspace": "/Users/…/workspace",
  "createdAt": "2026-11-10T02:11:03+00:00",
  "createdDate": "2026-11-10",
  "status": "recorded",
  "source": {"kind": "dir|git|git-dir", "path": "…", "url": "…", "ref": "…", "head": "…"},
  "initialSha": "<40位>",
  "fields": {"所属领域":"数据工程","子方向":"其他","Harness":"Claude Code",
             "操作系统":"MacOS/Linux","任务类型":"功能开发","任务难度":"困难",
             "语言/框架":"Python","环境可复现等级":"已容器化，可一键起环境"},
  "attempts": [{"index":1,"container":"solo2611-…","mode":"print","image":"…",
                "startedAt":"…","status":"accepted|rejected|cancelled|pending_collect",
                "dir":"…/monitor/runs/attempt-01","exitCode":0,"finishedAt":"…",
                "errors":[],"result":{…},"timeout":true}],
  "promptPath": "…/delivery/提示词.md",
  "promptSha256": "…",
  "promptReview": "…/monitor/prompt/ra-renhua-review.json",
  "difficultyReview": "…/monitor/prompt/difficulty-review.json",
  "difficultySignalsPassed": true,
  "remote": {"fullName":"o/r","web":"…","url":"…","branch":"main","private":false,
             "createdAt":"…","initialPushed":"…","artifactSha":"…","verified":true},
  "session": {"sessionId":"<uuid>","promptId":"…","harnessVersion":"2.1.303",
              "entrypoint":"…","models":["auto_model/urm"],"trace":"…/traces/…jsonl",
              "tracesDir":"…/traces","subagentFiles":0,"toolUses":142,
              "warnings":[],"mode":"print"},
  "artifact": {"sha":"…","modelCommits":[…],"shortStat":"18 files changed, …",
               "stat":"…","warning":"模型没有留下任何文件改动，产物快照等于初始快照"},
  "recording": {"video":"…/recording/运行录屏.mp4","durationSeconds":72,"width":1280,
                "height":720,"sizeMB":12.3,"needsObjectStorage":false,"source":"window",
                "finishedAt":"…","captureKind":"window-id","captureBackend":"screen-capture-kit",
                "windowCaptureOk":true,"pointerStrategy":"none","activationPerformed":false,
                "recordPlan":"…","recordPlanMode":"terminal"},
  "recordPlan": {…},
  "feedbackCheck": {"ok":true,"score":4,"warnings":[],"sha256":"…","checkedAt":"…"},
  "export": {"xlsx":"…","guide":"…","missing":[],"exportedAt":"…","exportedDate":"2026-11-11"},
  "pendingContainer": "solo2611-…"   // 仅 interactive 未 collect 时存在
}
```

**注意**：
- `artifact.warning` 存在时（模型没改任何文件），这题**基本作废**，界面应红字提示而不是当作成功。
- `session.models` 应为 `["auto_model/urm"]`；出现别的模型值说明镜像或配置不对。
- `recording.video` 是**绝对路径**，文件可能已被清理 → 判定时要 `is_file()`。
- 顶层没有 `updatedAt`；控制台自己维护（用文件 mtime 亦可）。

## 5. 磁盘产物清单

```
TASK_ROOT/
├─ workspace/                       git 仓库（bind 到容器 /workspace）
├─ traces/<sessionId>.jsonl         接受的会话轨迹（唯一一份）
├─ recording/
│   ├─ 运行录屏.mp4                  final 视频
│   ├─ raw.mov                       录制原始件（可删）
│   ├─ 运行录屏-window-capture.json  capture 报告：status/captureKind/ownerApp/width/height
│   ├─ 运行录屏-cursor-guard.json    cursor 报告：status/pointerStrategy
│   ├─ chrome-instance.json          web 题专用（6 项校验的来源）
│   ├─ chrome-profile-cleanup.json   同上
│   ├─ recorder.log
│   └─ .ready.json                   录制器就绪标记（内部用）
├─ delivery/
│   ├─ 题面草稿.md                   prompt --init 生成
│   ├─ 提示词.md                     prompt 安装后写入（安装即锁定）
│   ├─ 轨迹时间线.md                 digest 生成
│   ├─ feedback.md                   feedback --init 生成，人/Agent 写
│   ├─ 交付表.xlsx                   export 生成
│   ├─ 字段说明.md                   export 生成
│   └─ 产物材料.md                   bundle 生成（可选）
└─ monitor/
    ├─ state.json                    唯一事实源
    ├─ record-plan.json
    ├─ prompt/{difficulty-review.json, ra-renhua-review.json, validation.json}
    ├─ feedback-check.json
    ├─ runtime-verification.json     控制台的「本机复核」写这里（控制台独占）
    ├─ review-ack.json               控制台的「复核确认」写这里（控制台独占）
    ├─ chrome-session.json
    └─ runs/attempt-NN/{stdout.jsonl, stderr.log, traces/}
```

`monitor/` 下被控制台独占的两个文件（`runtime-verification.json`、`review-ack.json`）**技能不认识**，
写它们不会影响技能的判定，也不会被技能覆盖。这是控制台唯一可以自由写入技能目录的位置。

## 6. 对前序文档的勘误（已同步修改）

| 位置 | 原写法 | 实际 | 处理 |
|---|---|---|---|
| `01` FR-REP / `12` Q10 | 「每任务独立**私有**仓库」 | `repo` 默认**公开**，`--private` 才是私有；帮助文本写明「规范要求评测团队可访问」 | 改为「默认公开（规范要求）」，并注明不要加 `--private` |
| `02` 阶段 4 | 「逐行读 stdout → 解析 result 事件」 | 进度在 stderr，容器输出在两个文件，stdout 只有最后一次 emit | 改为双管道 + 双文件 tail |
| `02` §5 / `04` §2 | 「控制台维护槽位目录做互斥」 | 技能内部已有 `containers.lock` 与上限等待 | 改为「上限读取 config，槽位只用于展示，互斥交给技能」 |
| `02` §5.1 / `01` FR-REC-08 | 「控制台实现录屏串行锁」 | 技能已是主机级跨任务锁 | 改为「把技能错误原文翻译成提示」 |
| `01` FR-PRM-07 | 「自建相似题检测」 | 技能已内置 12-gram + difflib | 改为「展示技能 warnings」 |
| `04` §2 | 「草稿放 `drafts/` 不给技能看」 | `prompt --file` 接受任意路径，可行 | 保留，但注明技能自己的 `--init` 会写 `delivery/题面草稿.md`，控制台改用 `--file` 指到 `drafts/` |
| `11` §10 | 导出前 11 项检查 | 技能支持 `--allow-incomplete` 强出 | 明确控制台禁用该参数 |

## 7. 网关实现要点（可直接照抄的伪代码）

```python
class SkillGateway:
    def call(self, argv: list[str], *, timeout: float = 300) -> SkillResult:
        assert argv[0] in COMMAND_WHITELIST
        cmd = [sys.executable, "-I", str(self.script), *argv]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=self.clean_env(), cwd=self.skill_dir)  # cwd 固定，不继承用户目录
        return SkillResult(
            code=p.returncode,
            data=_try_json(p.stdout),        # 退出码 0/2 时才尝试解析
            out=p.stdout, err=p.stderr,      # 原文保留，供界面展示
            duration=…,
        )

    def stream(self, argv: list[str]) -> AsyncIterator[Event]:
        """run / interactive 用：同时读 stdout、stderr、attempt 目录下两个文件。"""
        # 1. stderr 逐行 → PhaseEvent（匹配 `[attempt N]`、`容器已达上限`、`不可用：`）
        # 2. attempt-NN/stdout.jsonl tail → ContainerLineEvent（每 200ms 合并）
        # 3. 进程退出 → 解析 stdout JSON → ResultEvent
```

`clean_env()` 只保留 `PATH`、`HOME`、`LANG`、`TMPDIR`、`ANTHROPIC_*` 之外的白名单，
其余（`AWS_*`、`SSH_*`、`GITHUB_TOKEN` 等）一律剔除。
