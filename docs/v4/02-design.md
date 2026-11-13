# v4 · 02 · 设计说明（数据层 → 操作层）

> 本文档是实现依据。规则来源见 `01-requirements.md` §3，冲突时以本文档为准。
> 文中 `skill:` 指 `~/.codex/skills/solo-2611`（发布时同步到 `~/.claude/skills/solo-2611`），
> `wb:` 指工作台仓库 `solo-auto`。

## 1. 总览

```
                       ┌──────────── 工作台 solo-auto ────────────┐
                       │ Dispatch Kernel → RunExecutor            │
                       │        │ 启动/轮询（不保持长连接）        │
                       │        ▼                                  │
                       │  SkillGateway ──run / run-status / resume │
                       └───────────────┬──────────────────────────┘
                                       │ 子进程（脱离）
                    ┌──────────────────▼──────────────────┐
                    │ 技能监督器 supervise（常驻）         │
                    │  状态机 / 判定 / 计数 / 事件日志     │  ← 每 30s 心跳写状态
                    │        │ tmux send-keys / capture-pane│
                    └────────┼────────────────────────────┘
                             ▼
                    tmux 会话 solo2611-<slug>-01-xxxx
                             │ docker run -it … interactive
                             ▼
                    容器：Claude Code 交互 REPL
                      题面 → 回复 → （继续）→ 回复 …
                             │ /home/node/.claude/projects
                             ▼ bind mount
                    monitor/runs/attempt-01/live/*.jsonl  ← 实时轨迹，判定依据
```

四层职责：

| 层 | 负责 | 明确不负责 |
|---|---|---|
| 容器（官方镜像） | 收题面、推理、改代码、回复 | 任何重试或会话管理 |
| 技能监督器 | 发题面、判轮次、发"继续"、计数、收尾、写状态与事件 | 调度、排队、并发决策 |
| 工作台调度 | 什么时候起哪道题、启动失败重试、终止后换题、容量与弹性 | 不猜会话内部状态，只读技能状态 |
| 人 | 待决策项、鉴权等待期处理、复核分数与文案 | 不手工发"继续"（除故障应急） |

**关键取舍（ADR）**

- **ADR-021 · 用受管交互取代无头模式，而不是改镜像。** 镜像入口拒绝会话恢复；改镜像或复用容器违反官方规范，
  轨迹里能看出 `entrypoint` 异常并存在被打回风险。代价是必须用 tmux 驱动 TUI。
- **ADR-022 · 监督器与调用方解耦。** 监督器是独立进程（`start_new_session=True`），调用方退出不影响它。
  "脚本断了导致作废"这一根因必须在技能侧消灭，工作台侧重连只是兜底。
- **ADR-023 · 以容器内轨迹 JSONL 为唯一判定依据，不用屏幕内容判定。** 屏幕是 TUI 渲染结果，难以稳定解析。
  为此新增一个只读用途的 bind mount（`projects` 目录），`docker cp` 保留作为收尾时的完整导出。
- **ADR-024 · 工作台不保持会话长连接。** 改为"发起 + 轮询"，后端重启零影响。
  这直接修掉 v3 认领逻辑里"找不到 rollout 就退避重试"的根本原因。
- **ADR-025 · 计数上限写死在技能。** 工作台只能调低。防止为了多产数据在界面上把上限改大。

## 2. 受管交互模式

### 2.1 启动（`skill: cmd_run` 重写，新增 `scripts/session.py`）

每次会话按 attempt 序号建目录，容器、tmux 会话、日志一一对应：

```
monitor/runs/attempt-01/
├── live/            # 新增：bind 到 /home/node/.claude/projects，实时轨迹
├── supervisor.log   # 监督器结构化日志（JSONL，一行一个事件）
├── supervisor.out   # 监督器自身 stdout/stderr（调试用）
├── tmux.buffer.log  # 收尾时最后一屏 capture-pane 全文（取证用）
└── traces/          # 收尾时整体复制（与现行一致，docker cp 或直接拷贝 live）
```

启动顺序：

1. `mkdir -p live`，`chmod 0777`；容器内 `node` 用户需要可写。
2. 生成容器名（沿用 `container_slug`）与 tmux 会话名 `solo2611-<slug>-<NN>-<4hex>`。
3. 用 `tmux new-session -d -s <会话名> -c <workspace> <启动脚本>` 起 tmux；启动脚本内容为
   `docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name <名>
   --label solo2611=true --label solo2611.task=<hash>
   --mount type=bind,src=<ws>,dst=/workspace
   --mount type=bind,src=<attempt>/live,dst=/home/node/.claude/projects
   -e apikey -e ANTHROPIC_BASE_URL <image> interactive`。
   Key 与网关地址不进 tmux 命令行（tmux 会把命令行写进 `ps`），改为先写一个 0600 的
   `monitor/runs/attempt-01/env.sh`，启动脚本 `set -a; . env.sh; set +a` 后执行，结束后删除。
4. 等 tmux 里出现输入框：轮询 `capture-pane` 直到匹配输入提示，或容器日志出现 REPL 就绪，最多 60 秒。
5. `tmux send-keys -l -- <题面原文>`，随后 `send-keys C-m`。题面逐字发送，不做任何转义改写；
   题面里如果有 `C-m` 之类的控制字符由 `-l` 保证按字面输入。
6. 写入 `state.sessionRun`（§4.1），`status=prompt_sent`。

启动失败判定：60 秒内输入框未就绪、`docker run` 立即退出、镜像缺失、入口拒绝启动——
一律 `start_failed`，删容器与 tmux，按 `claude.startAttempts`（默认 2）重新建**新容器**。
启动失败不算用掉机会（`01` Q2 默认）。

### 2.2 监视

三个信号源，优先级从高到低：

1. **实时轨迹**（唯一判定依据）：轮询 `<attempt>/live/-workspace/<sessionId>.jsonl`，文件大小变化即读取尾部。
   sessionId 未出现前扫目录找唯一的 `*.jsonl`。
2. **tmux 屏幕**（仅辅助）：`tmux capture-pane -p -t <会话名>` 取最后 15 行，判断输入框是否空闲、
   是否显示 `esc to interrupt`。用于"轨迹说结束了但界面还在转"这类矛盾时判断。
3. **进程与心跳**：`docker inspect` 的 `State.Running`、tmux `has-session`、监督器自身心跳。

**轮次结束判定** `round_ended(events)`：

```
取最后一条主链 assistant 事件 A（非 sidechain）。
若 A 之后还有主链 user 事件（tool_result 或真人消息）→ 未结束，继续等。
若 A 是 API 错误消息（isApiErrorMessage）→ 已结束，按 §3.2 分类。
若 A 的 stop_reason ∈ {end_turn, max_tokens, stop_sequence}
   且其 tool_use 都有对应 tool_result → 已结束。
另外要求：轨迹文件 10 秒内无增长、tmux 屏幕无 “esc to interrupt”。
两个条件都满足才进入分类；只有轨迹条件满足时，再等 20 秒复核一次。
```

### 2.3 发送"继续"

前置检查：屏幕最后 15 行显示输入框空闲，且没有 `esc to interrupt`。
发送：`send-keys -l '继续'` → `send-keys C-m`。只发这两个字，不追加换行、空格或提示。
发送后记事件 `continue.sent`（含 `sentBy=auto|human`、`reason`、`waitedMs`），并等轨迹出现新的真人消息。
若 3 分钟内轨迹无新真人消息：判 `unresponsive`，状态转 `manual_input`（§3.5），不发第二条。

### 2.4 收尾

1. 判定 `complete` → `status=closing`，连按两次 `C-d`（间隔 1 秒），等 tmux 会话消失，最多 60 秒。
2. 容器全部停止后，整体复制 `live/` 到 `traces/`（等价 `docker cp`，但更快且不依赖容器存活），
   若 `live/` 为空再回退 `docker cp`。
3. 进入现有 `_collect_container` 的校验（已按 §6.1 改写）。
4. 校验通过 → `_accept_attempt`（快照、digest、推送）→ `status=accepted`。
5. 校验不通过 → 按 §3.4 处理，通常是 `aborted`（不再有"整题重跑"）。
6. 无论成败，`tmux kill-session`、`docker rm`、删除 `env.sh`、删除 `live/` 的写权限
   （`chmod 0555`，防止容器残留进程写坏证据）。

### 2.5 失败与回退

- `claude.runMode = "print"` 时走旧的 `cmd_run`：一次机会、无"继续"、按现行为判定。
- 受管交互模式对新环境有前置检查：`tmux` 存在且版本 ≥ 3.0、非 Windows、`docker` 可用。
  任一不满足直接报错，不静默降级。
- 若在 T0.2 探针中发现"交互 REPL 在 API 错误后不接受输入"，回退方案是：中断后按 `Esc`
  一次再发"继续"；仍不可用则该类别转为终止（`01` Q4）。探针结论写入 `references/lessons-learned.md`。

## 3. 状态机

### 3.1 会话状态

`state.sessionRun.status` 取值与允许的迁移：

```
launching → prompt_sent → running ──┬→ continuing → running
        │                 │          ├→ holding ──┬→ continuing
        │                 │          │            └→ aborted
        │                 │          ├→ closing → accepted
        │                 │          ├→ aborted（终态）
        │                 │          └→ manual_input → continuing | aborted
        └→ start_failed（可回到 launching，受 startAttempts 限制）
prompt_sent/running 中容器非预期退出 → session_lost → accepted | aborted
任意状态监督器心跳超时 → lost（由 resume 接回，接不回则 aborted）
```

`accepted`、`aborted` 是终态，任何命令都不再改变它（`reset --yes` 也不行，`reset` 遇到终态直接拒绝）。

### 3.2 中断分类

`scripts/session.py` 新增 `classify_round(info, result, events)`，输入是 `trace.analyze` 的结果加尾部事件，
输出 `{kind, verdict, waitSeconds, detail}`。判定顺序自上而下，命中即停：

| # | 观察 | kind | verdict | 等待 |
|---|---|---|---|---|
| 1 | 最后 assistant 是 API 错误且含 429 / rate limit | `rate_limited` | continue | `max(Retry-After, 120)` 秒 |
| 2 | 最后 assistant 是 API 错误且含 5xx / overloaded / 网关 | `gateway` | continue | 60 秒（第 2 次 180 秒） |
| 3 | 最后 assistant 是 API 错误且含 401 / auth | `auth` | hold | 不自动发 |
| 4 | API 错误含 quota / 额度不足 | `quota` | hold | 不自动发 |
| 5 | 模型名不匹配（`unrecognized_model` 等） | `model_mismatch` | hold | 不自动发 |
| 6 | `stop_reason == max_tokens` | `truncated` | continue | 5 秒 |
| 7 | 存在没有结果的工具调用 | `truncated` | continue | 5 秒 |
| 8 | 回复命中 `CUT_OFF`（中断、超时、上下文耗尽痕迹） | `truncated` | continue | 5 秒 |
| 9 | 回复断在句子中间（`INCOMPLETE_TAIL`） | `truncated` | continue | 5 秒 |
| 10 | 回复为空 | `empty` | continue | 5 秒 |
| 11 | 回复命中 `ASKING_USER` 或 `PLAN_PROMISE` | `incomplete` | continue | 5 秒 |
| 12 | 以上都不是，且 `stop_reason == end_turn` | `none` | complete | — |

`stop_sequence` 但不满足上述条件时按 `truncated` 处理并告警（沿用现行 `completion_review` 的告警）。
判定函数复用 `trace.py` 的正则与逻辑，不复制一份，避免两处漂移。

### 3.3 "继续"预算与等待

```
已发次数 used ∈ {0,1,2}；上限 max = min(config.continueMax, 2)
verdict=continue 且 used < max  → 等待 → 发送 → used += 1
verdict=continue 且 used == max → abort(reason="continues_exhausted")
verdict=hold                    → 进入 holding，最多 fatalHoldMinutes（30 分钟）
                                  · 期间人工可执行 `continue`（计数）
                                  · 期间人工可执行 `abort`
                                  · 超时 → abort(reason="hold_timeout")
```

等待期间：容器保持运行，占用一个容器名额；容量页显示为"等待继续"；
无进展守护不把"等待继续"判为卡死（`02` §8.4）。

### 3.4 终止

`abort` 的权威入口只有一个内部函数 `session.abort(task_root, reason, by)`，所有终态都经它：

1. 在 tmux 里发 `C-c` 一次、两次 `C-d`，最多等 60 秒；不退出则 `docker stop -t 10` → `docker rm`。
2. 保存轨迹（能复制多少算多少）、`tmux.buffer.log`、最后 20 条事件。
3. 事件 `session.aborted`，`state.sessionRun.abort = {reason, at, by}`，`status=aborted`。
4. 写 `delivery/终止说明.md`：终止原因、已继续次数、中断原文、证据路径。
5. 不恢复工作区、不删除产物：现场留给人工排查。
6. 工作台据此把该题隔离、归还领取、工程进冷却。

### 3.5 需要人工输入（`manual_input`）

只在"发了继续但 3 分钟无反应"或 `hold` 期间人工介入时出现。此时监督器停止自动动作、只保活。
人在工作台点"人工继续"时，工作台调用 `continue --task-root ROOT`，等价于自动发送（计数）。
`manual_input` 超过 `fatalHoldMinutes` 未处理 → `abort(reason="manual_timeout")`。

### 3.6 与其他表层状态的映射

`state.status`（技能旧字段，工作台和 `status` 命令依赖）同步维护：

| sessionRun.status | state.status | state.attempts[-1].status |
|---|---|---|
| launching / prompt_sent / running / continuing / holding / manual_input / closing | `running` | `running` |
| start_failed（将再启动） | `running` | `start_failed` |
| accepted | `ran` | `accepted` |
| aborted | `session_aborted` | `aborted` |
| lost（监督器丢失且未接回） | `running` | `running`（`doctor` 可改 `lost`） |

`state.status=session_aborted` 是新增值，工作台 `derive_job_status` 需同步（§8）。

## 4. 数据层（技能）

### 4.1 `state.json` 增量

顶层新增 `sessionRun`（允许缺省，旧题不受影响）：

```jsonc
"sessionRun": {
  "schema": "solo2611/session-run/1",
  "mode": "supervised",
  "attemptIndex": 1,
  "container": "solo2611-…",
  "tmuxSession": "solo2611-…",
  "supervisor": {"pid": 12345, "pgid": 12345, "startedAt": "…", "heartbeatAt": "…"},
  "liveTraceDir": "…/monitor/runs/attempt-01/live",
  "status": "running",
  "startedAt": "…",
  "deadline": {"totalAt": "…", "noProgressAt": "…", "holdUntil": null},
  "continues": {"used": 1, "max": 2, "history": [
      {"no": 1, "at": "…", "sentBy": "auto", "reason": "rate_limited", "round": 1, "waitedMs": 121400}
  ]},
  "rounds": [
      {"no": 1, "startedAt": "…", "endedAt": "…", "stopReason": "error",
       "kind": "rate_limited", "verdict": "continue", "detail": "429 Tokens Per Minute",
       "toolUses": 87, "finalChars": 0},
      {"no": 2, "startedAt": "…", "endedAt": "…", "stopReason": "end_turn",
       "kind": "none", "verdict": "complete", "toolUses": 41, "finalChars": 812}
  ],
  "interrupts": [{"round": 1, "kind": "rate_limited", "at": "…"}],
  "blocked": null,                       // holding 时 {kind, since, until}
  "abort": null,                         // {reason, at, by, detail}
  "outcome": null                        // accepted 时 {acceptedAt, remark}
}
```

`state.attempts[-1]` 增加：`mode: "supervised"`、`rounds: N`、`continues: N`、`startFailed: bool`、
`finishedAt`、`exitCode`（容器退出码，能取到才写）。

写入约定：
- 所有写 `state.json` 的操作走 `common.save_state`，新增 `atomic` 实现（写临时文件 + `os.replace`）。
- 技能的写与工作台的读之间用现成的 flock（`file_lock(state.lock)`），新增 `state.lock`；
  读方重试 3 次、每次 50ms，失败则返回上一次内容并标 `stale`。

### 4.2 事件日志 `supervisor.log`

一行一个 JSON，追加写，只增不改：

```json
{"at":"2026-11-13T09:10:02+08:00","round":1,"event":"round.end","kind":"rate_limited","verdict":"continue","detail":"429 Tokens Per Minute","waitSeconds":120}
{"at":"...","round":1,"event":"continue.sent","no":1,"sentBy":"auto","waitedMs":121400}
{"at":"...","round":2,"event":"round.end","kind":"none","verdict":"complete"}
{"at":"...","event":"session.closing"}
{"at":"...","event":"session.accepted","sessionId":"...","promptId":"..."}
```

事件类型固定集合：`session.start`、`prompt.sent`、`round.start`、`round.end`、`continue.sent`、
`hold.start`、`hold.clear`、`manual_input`、`session.closing`、`session.accepted`、
`session.aborted`、`session.lost`、`supervisor.heartbeat`（60 秒一次，可被 grep 忽略）。
工作台、`run-status`、时间线、诊断都读这一份，不再各自解析别的文件。

### 4.3 备注

有 1 次及以上"继续"且最终被接受时：

```
模型运行中途中断，人工输入"继续"N次后完成
```

- 同时命中明文 Key 时：`模型运行中途中断，人工输入"继续"N次后完成；轨迹中有存在明文apikey暴露风险`。
- 写两个位置：`delivery/交付表.xlsx` 的「备注」列、平台 `other_issues`。
- `submission.py` 现在把 `other_issues` 放在 `EXCLUDED_SUBMISSION_FIELDS` 并在仅 Key 风险时写回（`submission.py:591`）。
  改为：`_submission_remark(state)` 统一返回合并后的字符串，两处（新建与返修 `:722`）都调用它。
- `feedback --check` 不检查备注内容；备注不进入 Feedback 正文（正文仍禁止工作流信息）。

### 4.4 并发与锁

| 锁 | 路径 | 作用 |
|---|---|---|
| `containers.lock` | `~/.codex/solo-2611/` | 现有，容器上限 |
| `launch.lock` | 同上 | 新增，全局启动间隔：文件内记上次启动的单调时钟与墙钟，两次启动至少间隔 `launchSpacingSeconds` |
| `state.lock` | `TASK_ROOT/monitor/` | 新增，状态文件读写互斥 |
| 监督器单例 | `TASK_ROOT/monitor/.supervisor.lock` | 内容 `pid:pgid:startedAt`；启动前检查 pid 是否存活且命令行含 `supervise --task-root <root>` |

启动间隔默认 20 秒，与工作台 `dispatch.launch_spacing_seconds=45` 不冲突（技能是下限，工作台是主控）。

### 4.5 目录增量

```
TASK_ROOT/
├── monitor/runs/attempt-01/{live, supervisor.log, supervisor.out, tmux.buffer.log, env.sh(临时), traces/}
└── delivery/终止说明.md        # 仅终止的题
```

## 5. CLI 契约增量

技能版本升至 `0.6.0`，`VERSION.json` 的 `version` 同步。

```
run --task-root ROOT [--force] [--follow] [--mode supervised|print]
     受管模式默认脱离：打印 {"ok":true,"supervised":true,"attempt":1,"container":"…",
     "tmux":"…","supervisor":12345,"next":"run-status"} 后立即退出。
     --follow 时持续把 supervisor.log 的新行打到 stderr，直到终态，最后输出结果 JSON。
run-status --task-root ROOT [--json] [--lines N]
     人读文本：状态、第几轮、继续 x/2、最近活动、下一步、剩余时间。
     --json：{"status":"running","round":2,"continues":{"used":1,"max":2},
              "lastKind":"rate_limited","nextActionAt":"…","remainingSeconds":4120,
              "supervisorAlive":true,"container":"…","sessionId":"…","logTail":[…]}
resume --task-root ROOT [--follow]
     幂等：监督器活着 → 只返回当前状态；监督器死了但 tmux 与容器还在 → 起新监督器接回（不计数）；
     tmux 没了但容器还在 → 新建 tmux 附着（docker attach 不允许，此时按 lost → abort）。
continue --task-root ROOT [--note TEXT]
     人工发一次"继续"，计数；--note 只进事件日志与 supervisor.log，不发给模型。
abort --task-root ROOT --reason TEXT [--by human|auto]
run --follow 之外，提供 resume-all [--tasks-dir DIR] [--dry-run]
doctor --fix-stale [--tasks-dir DIR]
supervise --task-root ROOT --attempt N        # 内部入口，不建议手工调用
```

退出码沿用：0 成功、1 业务失败、2 门禁未通过。`run-status` 在 `status=aborted` 时退出码 0
（查询本身成功），是否终止看 JSON 字段。

## 6. 交付链变更（技能侧）

### 6.1 轨迹校验 `trace.analyze`

现在是"真人 prompt 恰好 1 条"（`trace.py` `analyze`：`len(prompts) != 1` 报错）。改为：

```
第一轮真人消息必须与 delivery/提示词.md 规范化后一致，且带 promptId → 取为提交用 promptId。
其余真人消息必须严格等于 "继续"（去空白后），条数 ≤ 2，且每条都有 promptId。
若出现其他内容的真人消息 → 错误（说明有人干预）。
"继续"之后必须有新的 assistant 轮次，否则视为未使用。
```

`is_real_user_prompt` 判定不变（`继续` 是普通 user 事件，会被计入）。
`completion_review` 只看最后一轮：`finalText` 取最后一条主链 assistant，`dangling` 只看最后一轮的
工具调用（按最后一个真人消息切分），否则前面轮次的悬空调用会误报。

### 6.2 轨迹时间线

`trace.py` 的时间线生成增加中断标记：在 `round.end` 处输出一行
`⏸ 第 N 轮中断（kind）→ 等待 Xs → 输入"继续"（第 M 次）`，并在文件头写
`会话共 N 轮，继续 M 次（原因：…）`。字数与格式不影响现有门禁。

### 6.3 Feedback 与评分

- `feedback.py` 的检查不因"继续"阻断；但 `--check` 增加一条提示：若 `sessionRun.continues.used > 0`，
  且 `score == 5`，而任一中断 `kind ∈ {truncated, incomplete, empty}`，报错阻断（对应 FR-S8）。
- `references/feedback-guide.md` 与 `score-calibration.md` 增加对应条款。
  因限流/网关中断的，不扣分也不得作为"效率差"的证据。

### 6.4 export / submit

- `build_row` 的「备注」取 §4.3 的合并串。
- `submit` 与 `repair` 用同一个 `_submission_remark`。
- 导出前置检查增加：`sessionRun` 为终态才允许导出；`aborted` 的题不允许导出（与现在"缺项即拒绝"一致）。

## 7. 工作台数据层

### 7.1 SQLite 迁移（`store/repo.py` 的 `schema_version` 增第 4 版）

```sql
-- 4.1 尝试表补充会话维度
ALTER TABLE attempts ADD COLUMN session_run_status TEXT;   -- sessionRun.status
ALTER TABLE attempts ADD COLUMN rounds INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN continues INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN start_failed INTEGER DEFAULT 0;
ALTER TABLE attempts ADD COLUMN interrupt_kind TEXT;       -- 最后一次中断类别
ALTER TABLE attempts ADD COLUMN last_activity_at TEXT;

-- 4.2 轮次明细（界面时间线与统计用）
CREATE TABLE IF NOT EXISTS session_rounds (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_seq   INTEGER NOT NULL,
  round_no      INTEGER NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  stop_reason   TEXT,
  kind          TEXT NOT NULL,          -- rate_limited | gateway | truncated | incomplete | empty | none | auth | …
  verdict       TEXT NOT NULL,          -- complete | continue | hold | abort
  detail        TEXT,
  tool_uses     INTEGER DEFAULT 0,
  final_chars   INTEGER DEFAULT 0,
  continued     INTEGER DEFAULT 0,      -- 该轮后是否发了继续
  UNIQUE (task_id, attempt_seq, round_no)
);

-- 4.3 调度项补充
ALTER TABLE dispatch_items ADD COLUMN session_state TEXT;     -- running | waiting_continue | holding | manual_input
ALTER TABLE dispatch_items ADD COLUMN continue_used INTEGER DEFAULT 0;
ALTER TABLE dispatch_items ADD COLUMN next_action_at TEXT;    -- 下次自动动作时间（等待后继续）
ALTER TABLE dispatch_items ADD COLUMN container_seconds INTEGER DEFAULT 0;

-- 4.4 容量时间线补一列
ALTER TABLE dispatch_capacity ADD COLUMN waiting INTEGER DEFAULT 0;
```

`attempts.outcome` 枚举扩展：`accepted | rejected | cancelled | timeout | aborted | start_failed | lost`。
`error_kind` 扩展：`continues_exhausted | run_timeout | no_progress | hold_timeout | manual_timeout |
session_lost | unresponsive | auth | quota | model_mismatch`。

**索引不重建，只加列**：`idx_tasks_session` 等既有约束保持不动，迁移可重入（`INSERT OR IGNORE` 到 `schema_version`）。

### 7.2 状态映射（替换 `docs/04` §4 表的相关行）

| 技能状态 | 条件 | job_status | 展示 |
|---|---|---|---|
| `running` | `sessionRun.supervisorAlive=true` | `running` | 「运行中 00:34:12 · 第 2 轮 · 继续 1/2」 |
| `running` | 监督器心跳超时且 tmux 存在 | `running` | 先自动 `resume`；接回失败 → `blocked`「监督器丢失」 |
| `running` | 容器与 tmux 都不存在 | `blocked` | 「会话丢失」 |
| `running` | `sessionRun.status=holding` | `blocked` | 「等待人工处理：鉴权」+ 按钮 |
| `running` | `sessionRun.status=manual_input` | `blocked` | 「需要人工输入」 |
| `session_aborted` | `reason=continues_exhausted` | `aborted` | 「继续用尽，需换题」 |
| `session_aborted` | 其他 | `aborted` | 原因原文 |
| `ran` | 有其他会话但本题未成立 | `ready` | 启动失败后重新排队 |

### 7.3 磁盘增量

```
~/solo-auto/run/sessions/<task_id>.json     # 工作台侧影子状态（最近一次 run-status 快照，只读用途）
~/solo-auto/logs/skill/<task_id>/run-status-<ts>.jsonl   # 轮询日志，7 天清理
```

影子状态不参与判定（磁盘上的 `state.json` 仍是唯一事实源），只用于界面快速渲染与后端重启后立即显示。

## 8. 工作台调度层

### 8.1 执行器改造（`pipeline/scheduler.py`）

- `_execute` 不再 `await gateway.stream(run …)`。改为：
  1. `gateway.call(["run", "--task-root", …])`，期望 3 秒内返回；返回失败按 `start_failed` 分类。
  2. 进入轮询循环：每 5 秒 `run-status --json`，把 `rounds/continues/nextActionAt` 写回影子状态与库，
     有新事件就 `bus.publish("run.log", …)`（界面的实时日志改读 `supervisor.log` 尾部）。
  3. `status ∈ {accepted, aborted}` → 退出循环，走 `_finish`。
  4. 连续 3 次 `run-status` 失败（技能进程异常）→ 调 `resume`，仍失败 → 交守护处理（只观察，不擅自杀容器）。
- 工作台**不主动**终止会话；终态只由技能产生。唯一例外：`abort` 接口（人工点"放弃"）。

### 8.2 重启认领（`pipeline/reconcile.py`）

`_current_container` 与"找不到 rollout 就退避重试"的逻辑改为：

```
对每个 state.status == running 的任务：
  status = run-status --json
  supervisorAlive ? 更新影子状态并继续轮询
                  : 调 resume；成功则继续，失败则标 blocked(session_lost) 并告警
```

不重启、不重新建容器、不计入任何次数。这同时解决了 v3 `implementation-status.md` 里
「v2 运行进程认领未完整」的遗留（在 v4 范围内只做会话层认领，不做 v2 引擎的认领）。

### 8.3 失败策略（`dispatch/default_patterns.toml` 与 `03-failure-playbook.md` §2.3 同步）

| 类别 | charge | max | 处置 |
|---|---|---|---|
| `run.start_failed` | 否（独立计数） | 2 | 退避 30/60 秒后重新 `run`（新容器） |
| `run.continues_exhausted` | 否 | 0 | 隔离 + 归还领取 + 工程冷却 |
| `run.timeout` | 否 | 0 | 隔离 + 冷却 |
| `run.no_progress` | 否 | 0 | 隔离 + 冷却 |
| `run.hold_timeout` | 否 | 0 | 隔离 + 冷却 |
| `run.session_lost` | 否 | 1 | 先 `resume`；接不回 → 隔离 |
| `run.unresponsive` | 否 | 0 | 转 `blocked`，等人；超时 → 隔离 |
| `gateway.auth` / `gateway.quota` / `gateway.model_mismatch` | 否 | 0 | 现有全局/容器级挂起不变（对话仍保留） |
| `gateway.rate_limited` / `gateway.timeout` | — | — | **不再作为任务失败上报**，只记 `session` 事件并驱动弹性与探测 |

`gateway.*` 的"会话内"情形与"发起前"情形的区分：由 `run-status` 的 `lastKind` 判断，
任务本身仍在运行的，交给技能处理，调度只记账不拦截。

### 8.4 守护与容量

- 无进展守护（`guards/progress.py`）：进展信号包含 `supervisor.log` 的最后修改时间、实时轨迹的增长、
  `state.json` 的 `updatedAt`。`session_state ∈ {waiting_continue, holding}` 期间不计入无进展，
  改由技能自己的 `noProgressAt`/`holdUntil` 计时。
- 运行超时守护：以 `sessionRun.deadline.totalAt` 为准，而不是工作台自己的计时。
- 容量账本（`dispatch/pools.py`）：`waiting_continue` 与 `holding` **计入占用**。
  `dispatch_capacity.waiting` 独立统计，容量页显示"占用 3（运行 2 / 等待 1）"。
- 弹性（`dispatch/elastic.py`）：限流发生时，容器的"停顿"计入 `stall`，用于下调；
  由于容器不退出，不能用"容器消失"作为下调信号，要读 `session_state=waiting_continue` 的持续时长。

### 8.5 启动间隔与批量并发

沿用 `dispatch.launch_spacing_seconds`。技能侧新增的 `launch.lock` 是第二道保险，
专门拦 Agent 直接调技能、绕开工作台的情形（对应 `01` §2.2 根因三）。

## 9. 工作台接口

新增（`api/tasks.py` 与 `api/dispatch.py`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/tasks/{id}/run/resume` | 接回；返回 `run-status` |
| POST | `/api/tasks/{id}/run/continue` | 人工发一次"继续"（计数，需二次确认） |
| POST | `/api/tasks/{id}/run/abort` | 放弃并终止 |
| GET | `/api/tasks/{id}/run/session` | 轮次、继续、中断、剩余时间 |
| GET | `/api/dispatch/session-stats` | 继续率、终止原因分布、每容器小时产出 |
| POST | `/api/dispatch/queue/{task_id}/resume` | 队列项批量接回 |

SSE 事件：`session.round_end`、`session.continue_sent`、`session.held`、`session.aborted`、
`session.accepted`。字段与会话轮次表一致。

`GET /tasks/{id}/run` 的返回结构（现有 `scheduler.run_status`）扩展 `round/continues/next_action_at/
session_state`，旧字段保留，前端可渐进迁移。

## 10. 工作台界面

**任务详情页新增「会话」面板**（`components/task/SessionPanel.tsx`）：

```
第 2 轮 · 已继续 1/2              [接回] [继续] [放弃]
──────────────────────────────────────────────────
轮 1  09:10:02  429 限流       等待 121s → 发送"继续"(1/2)
轮 2  09:12:03  进行中         已产生 41 次工具调用
──────────────────────────────────────────────────
下次动作：自动（剩余 2h 41m 后终止）     监督器：在线（心跳 12s 前）
```

- `holding` 时面板变黄，显示原因与倒计时，主按钮为「处理完继续」。
- `aborted` 时变红，显示终止原因与证据路径，主按钮为「换题」。
- 「继续」按钮点击后弹二次确认：`此操作会消耗 1 次继续机会（剩余 x 次）`。

**调度页**：容量卡显示"占用 3（运行 2 / 等待 1）"；新增继续率、终止原因分布两个小卡。
**设置页**：新增运行模式、继续上限（0–2）、等待处理时限、无进展时限；移除"尝试次数"
（读到旧值仅提示"v4 起固定为 1 次会话"）。

## 11. 运维手册（runbook）

| 场景 | 判断 | 处理 |
|---|---|---|
| Agent 的命令超时了，题还在跑 | `run-status` 显示 `supervisorAlive=true` | 直接对 Agent 说"继续"，Agent 执行 `resume`；不消耗次数 |
| `run-status` 显示监督器不在，tmux 在 | 心跳超时 | `resume`，它会新建监督器接回 |
| `run-status` 显示 `manual_input` | 发了继续没反应 | 打开 `tmux attach -t <会话名>` 人工看一屏，必要时人工输入（计入次数） |
| `holding`（401 / 额度） | 网关问题 | 修 Key 或等额度恢复 → 工作台点"处理完继续"（计数）；超过 30 分钟自动终止 |
| 想立刻停一道题 | — | `abort --reason 人工放弃`；不要 `docker stop`（会丢现场） |
| 升级技能 | — | 先发布到 `~/.codex` 与 `~/.claude` 两处，再重启工作台后端；正在跑的会话不受影响 |
| 想整题重开 | — | v4 不支持。加 `--force` 也只对新题生效；已终止的题 `reset` 会被拒绝 |

## 12. 风险与降级

| 风险 | 影响 | 降级 |
|---|---|---|
| 交互 TUI 无法用 `send-keys` 稳定输入中文 | 题面发不出去 | T0.2 探针先验证；失败则题面改走"写文件 + 提示打开文件"（会改题面规范，需项目方同意），或回退 `print` |
| API 错误后 REPL 不接受输入 | 无法继续 | 先 `Esc` 再发；仍不行则该类别转终止 |
| 挂载 `projects` 目录被入口拒绝 | 无法实时判定 | 回退为 5 秒一次 `docker exec cat` 轮询 + `docker cp` |
| 平台质检对多轮轨迹有异议 | 数据打回 | 先小批量试水（Q1）；备注必填；随时可切 `print` |
| 监督器自身崩溃 | 会话悬空 | 心跳 + `resume`；工作台每 60 秒兜底扫描一次 |
| tmux 里 Key 泄露 | 安全 | `env.sh` 0600 且用后删除；`ps` 不含 Key |
| 容器长时间填满名额 | 产能下降 | 总时长上限 10800 秒 + 无进展 20 分钟；容量页显示等待项 |
