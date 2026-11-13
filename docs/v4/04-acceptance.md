# v4 · 04 · 验收文档

## 1. 总则

### 1.1 验收什么

不是"功能都在"，而是四条：

1. **不重跑**：任何中断都不再导致整题作废，除非规则明确要求终止。
2. **不误判**：把"正常的慢"当成中断、把"中断"当成完成，都是严重缺陷。
3. **不造假**：备注、轮次、继续次数、交付表、平台字段必须和轨迹与事件日志一致。
4. **可接回**：监督器、Agent、工作台后端任一进程被杀，会话都能接回且不消耗次数。

### 1.2 自动化程度

| 标记 | 含义 |
|---|---|
| **自** | 必须有自动化测试，进 `make check` 或技能侧 `pytest` |
| **演** | 用演练脚本在隔离目录执行，留日志 |
| **真** | 必须在真实技能 + 真实 Docker + 真实镜像上人工执行一次 |

所有演练在隔离环境：技能 `SOLO2611_TASKS_DIR=/tmp/solo2611-accept`，工作台
`SOLO_AUTO_HOME=/tmp/solo-auto-accept`。不触碰 `~/claude-runs/solo-2611` 与 `~/solo-auto`。

### 1.3 静态检查增量（工作台）

沿用 v1–v3 全部规则，追加 4 条 grep：

1. `dispatch/` 之外不得出现 `docker ps` 直接调用（统一经 `facts.py`）。
2. 后端不得出现直接改写任务 `state.json` 的代码（只允许调技能命令）；例外白名单为空。
3. 不得出现字面量 `continues_max = 2` 以外的上限赋值，也不得读取配置后写入大于 2 的值。
4. `session_rounds` 的写入必须来自技能事件，不得由前端或 API 凭空生成。

技能侧追加 2 条：

5. `session.py` 中 `send-keys` 的文本必须是常量 `"继续"`，不得拼接任何变量（除题面首次发送）。
6. `abort` 只能由 `session.abort` 一个函数执行，其他位置不得直接 `docker stop` / `docker rm` 会话容器。

---

## 2. P0 用例

### 2.1 基线（BL）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| BL1 | 演 | 指向真实 tasksDir 的只读副本 | `doctor baseline --tasks-dir <副本> --out monitor/baseline.json` | 输出含：尝试状态分布、失败原因分类计数、容器总时长、每容器小时产出、未产出数据的容器时长占比；与 `01` §2.1 的 258/73/93/79 一致；不改动任何原始文件 |

### 2.2 交互探针（PR）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| PR1 | 真 | 隔离目录，已 `init` + `prompt` | `probe_interactive --task-root ROOT` | 三项结论全部记录：① 中文题面经 `send-keys -l` 后容器内 REPL 显示逐字一致（含标点、换行、引号）；② `/home/node/.claude/projects` bind 挂载成功且轨迹实时出现；③ 发一条消息后 `Ctrl-C`，再发"继续"，REPL 接受并继续推理。任一项为否，结论写入 `lessons-learned.md` 并触发 `02` §12 的降级方案 |

### 2.3 启动间隔（SP）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| SP1 | 自 | `launchSpacingSeconds = 20` | 连续调两次 `run`（不同任务） | 第二个在第一个启动后 ≥ 20 秒才起容器；`launch.lock` 文件内记有上次时间 |
| SP2 | 自 | 同 SP1 | 把配置改为 5，再改回 20 | 读配置即时生效；上限不得低于 5 |
| SP3 | 真 | 隔离目录 | `run` 后立刻 `docker ps -a --filter label=solo2611=true` 与 `ps -eo command \| grep docker` | tmux 与 `ps` 输出中**不出现** API Key 明文；`env.sh` 权限 0600 且会话结束后已删除 |

### 2.4 静默超时（TO）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| TO1 | 自 | 假 stdout 文件，30 分钟无新增 | 计时器推进到 `silentSeconds=1200` | 判定为静默超时；事件写 `stalled`；不再用固定 7200 秒 |
| TO2 | 自 | 假 stdout 持续有新行，但总时长已达 `timeoutSeconds` | 计时器推进 | 判定为总时长超时；终止原因与静默超时可区分 |

### 2.5 Ctrl-C（CC）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| CC1 | 演 | 真实 `run` 进行中 | 对技能进程发 `SIGINT` | 进程退出但**不** `docker stop`、**不** `_reset_workspace`；容器仍在跑；`state.json` 的 `status` 仍为 `running`；打印接回指令 |
| CC2 | 演 | 同 CC1 | 加 `--hard-stop` 再发 `SIGINT` | 立即停容器、恢复工作区、`status=prompt_ready`（旧行为，用于明确要放弃时） |

### 2.6 print 版接回（RS）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RS1 | 演 | `run` 进行中，Python 进程已被杀，容器仍在 | `resume --task-root ROOT` | 接回并 `docker wait`；结束后正常走收集与判定；产出 `accepted` 或按原规则 `rejected` |
| RS2 | 演 | 进程被杀，容器已退出未删 | `resume` | 直接走 `_collect_container`，不重启容器 |
| RS3 | 演 | 进程被杀，容器已被外部删除 | `resume` | 尝试标 `lost`，提示 `reset --yes`；不自动重跑 |
| RS4 | 自 | `state.json` 里两条 `running` 尝试（异常数据） | `resume` | 只处理最后一条，并在事件里记录异常 |

---

## 3. P1 用例（受管交互与"继续"）

### 3.1 会话建立（SG）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| SG1 | 演 | 隔离目录，镜像已就绪 | `run` | 3 秒内返回 JSON `{supervised:true, attempt, container, tmux, supervisor}`；进程已退出但监督器与容器存活 |
| SG2 | 演 | 同上 | 60 秒内 `run-status --json` | `status=prompt_sent` 或 `running`；轨迹目录 `live/` 有 JSONL；容器内轨迹首条真人消息与 `提示词.md` 规范化后逐字一致 |
| SG3 | 演 | 人为让入口拒绝（例如同名容器已存在） | `run` | 判 `start_failed`；删除残留 tmux 与容器；按 `startAttempts` 重新起**新容器**；不消耗会话机会 |

### 3.2 实时轨迹（TR）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| TR1 | 自 | `live/` 中有一个 JSONL，大小持续增长 | 监督器读取 | 只读新增部分，不全量重读；CPU 占用在 8 小时演练中低于单核 5% |
| TR2 | 演 | `live/` 为空或半行 JSON | 监督器读取 | 跳过不完整行，不抛异常；文件后续被补全后能正常解析 |

### 3.3 轮次结束判定（RE）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RE1 | 自 | 轨迹末尾 `stop_reason=end_turn`，无悬空工具调用，文件静默 10 秒，屏幕空闲 | 判定 | `round_ended=true` |
| RE2 | 自 | 同上但屏幕显示 `esc to interrupt` | 判定 | `round_ended=false`，继续等 |
| RE3 | 自 | 最后一条 assistant 后有 `tool_result` 再到下一条 assistant | 判定 | `false`，不提前判结束 |
| RE4 | 自 | 最后一条 assistant 有 `tool_use` 但无对应 `tool_result` | 判定 | `true`，且分类为 `truncated` |
| RE5 | 演 | 模型长时间思考（>20 分钟无新行） | 判定 | 不误判结束；由无进展计时接管 |

### 3.4 中断分类（CLS）：表驱动，一条一类

| # | 构造（轨迹尾部） | 期望 kind | verdict |
|---|---|---|---|
| CLS1 | API 错误 `429 … Tokens Per Minute` | `rate_limited` | continue |
| CLS2 | API 错误 `503 overloaded` | `gateway` | continue |
| CLS3 | API 错误 `401 authentication` | `auth` | hold |
| CLS4 | API 错误 `quota exceeded` | `quota` | hold |
| CLS5 | 轨迹含 `unrecognized_model` | `model_mismatch` | hold |
| CLS6 | `stop_reason=max_tokens` | `truncated` | continue |
| CLS7 | 悬空工具调用 | `truncated` | continue |
| CLS8 | 回复含"上下文已满/请求被中断" | `truncated` | continue |
| CLS9 | 回复以"、"或"and"结尾 | `truncated` | continue |
| CLS10 | 回复为空 | `empty` | continue |
| CLS11 | 回复尾句"需要我继续吗？" | `incomplete` | continue |
| CLS12 | 正常 `end_turn` 有正文有工具调用 | `none` | complete |

补充：CLS13（自）优先级顺序——同时命中 429 与截断特征时判 `rate_limited`；
CLS14（自）`stop_sequence` 且不满足任何特征 → `truncated` + 告警。

### 3.5 状态机与预算（SM / CN）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| SM1 | 自 | `continues.used=0, max=2`，classify 返回 continue | 一拍 | 等待后发送"继续"；`used=1`；事件 `continue.sent` |
| SM2 | 自 | `used=2`，再次 continue | 一拍 | `abort(reason=continues_exhausted)`；不再发送 |
| SM3 | 自 | `used=1`，classify 返回 `auth` | 一拍 | 进入 `holding`；`blocked={kind:auth, until:+30m}`；不发送 |
| SM4 | 自 | `holding` 且已超 30 分钟 | 一拍 | `abort(reason=hold_timeout)` |
| SM5 | 自 | `holding` 中人工执行 `continue` | 一条命令 | 发送一次，`used+1`，`sentBy=human` |
| SM6 | 自 | 任意运行态，执行 `abort` | 一条命令 | 进入 `closing → aborted`；写 `终止说明.md`；`status=session_aborted` |
| SM7 | 自 | 已 `accepted` | 执行 `abort` / `continue` / `reset --yes` | 全部拒绝，终态不可变 |
| SM8 | 自 | 监督器心跳停止 120 秒 | 一拍 | `status=lost`（不进 `aborted`），等 `resume` |
| CN1 | 演 | 真实 429（用错误 Key 或并发压测触发） | 自动运行 | 等待 ≥ `Retry-After`；发出字面"继续"；轨迹里出现内容恰为"继续"的第二条真人消息 |
| CN2 | 自 | `continueMax` 配置为 5 | 启动会话 | 实际生效上限为 2；启动时告警一次 |
| CN3 | 自 | 发出"继续"后 3 分钟内无新真人消息 | 一拍 | 转 `manual_input`；不再自动发第二条；事件 `manual_input` |
| CN4 | 演 | `manual_input` 中，在 tmux 里人工输入内容并发送 | 一拍 | 监督器检测到新增真人消息；若内容不是"继续"则记告警并计入 `interventions`；会话继续被监视 |

### 3.6 监督器（SV）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| SV1 | 演 | 会话运行中 | 手工 `kill -9` 监督器 | 心跳停止；60 秒内 `resume` 起新监督器；`used` 不变；会话继续 |
| SV2 | 自 | 已有监督器存活 | 再次 `run` 或 `supervise` | 拒绝并返回现有 pid；不产生第二个监督器 |

### 3.7 run 与收集（RN）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RN1 | 演 | 隔离目录 | `run --mode print` | 走旧逻辑；一次机会；无"继续"；`state.sessionRun.mode=print` |
| RN2 | 演 | 受管模式正常完成 | 收尾 | 两次 `C-d` 后 60 秒内会话结束；`traces/` 有完整轨迹；容器已删；`env.sh` 已删 |
| RN3 | 演 | 收尾时 `live/` 复制失败 | 收尾 | 回退 `docker cp`；仍失败则保留容器并报错，不静默当成功 |

### 3.8 轨迹校验（VA）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| VA1 | 自 | 1 条题面 + 0 条继续 | `analyze` | 通过；`promptId` 取题面那条 |
| VA2 | 自 | 1 条题面 + 2 条"继续" | `analyze` | 通过；事件里记录 `continues=2` |
| VA3 | 自 | 1 条题面 + 3 条"继续" | `analyze` | 不通过（超出上限） |
| VA4 | 自 | 1 条题面 + 1 条"帮我再改改" | `analyze` | 不通过（人为干预）；错误文案点明"出现非'继续'的真人输入" |
| VA5 | 自 | 前一轮有悬空工具调用，最后一轮干净 | `completion_review` | 只看最后一轮，不因前一轮悬空而报错 |

### 3.9 命令行（CLI）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| CLI1 | 自 | 会话运行中 | `run-status`（人读） | 含状态、第几轮、继续 x/2、最近活动、下一步、剩余时间 |
| CLI2 | 自 | 同上 | `run-status --json` | 字段齐全且可被 `json.loads` 解析；`supervisorAlive` 与实际一致 |
| CLI3 | 自 | 监督器已死 | `resume --follow` | 起新监督器并持续输出直到终态 |
| CLI4 | 自 | 3 道题不同状态 | `resume-all --dry-run` | 只列不改；不加 `--dry-run` 时逐一接回 |
| CLI5 | 自 | 人为构造 `status=running` 且容器不存在的旧数据 | `doctor --fix-stale` | 标 `lost`；不改其他任务 |
| CLI6 | 自 | `aborted` 任务 | `run` | 拒绝，提示"已终止，需换题" |
| CLI7 | 自 | 无 `--task-root` | 任意新命令 | 报错并给出用法；退出码 1 |

### 3.10 事件与时间线（LG）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| LG1 | 自 | 一次含 1 次继续的完整会话 | 检查 `supervisor.log` | 事件序列为 `session.start → prompt.sent → round.start → round.end(rate_limited) → continue.sent → round.start → round.end(none) → session.closing → session.accepted`；每条一行合法 JSON |
| LG2 | 演 | 同 LG1 | `digest` | `轨迹时间线.md` 头部写"会话共 2 轮，继续 1 次（原因：限流）"；中断处有一行 ⏸ 标记；现有门禁不受影响 |

### 3.11 备注与提交（RM）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RM1 | 自 | 继续 1 次 + 无 Key 风险 | `export` | 「备注」= `模型运行中途中断，人工输入"继续"1次后完成` |
| RM2 | 自 | 继续 2 次 + 有 Key 风险 | `submit --dry-run` | `other_issues` 两句用中文分号连接；备注列同 |
| RM3 | 自 | 继续 0 次 + 无 Key 风险 | `export` | 备注为空；`other_issues` 被剔除（沿用现规则） |

### 3.12 评分（SC）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| SC1 | 自 | `continues=1`，中断 kind=`truncated`，分数 5 | `feedback --check` | 阻断，提示"模型自身中断过的题不得评 5" |
| SC2 | 自 | `continues=1`，中断 kind=`rate_limited`，分数 5，满分条件齐全 | `feedback --check` | 通过（限流不扣分） |

### 3.13 文档（DOC）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| DOC1 | 自 | — | 全文检索 `SKILL.md` | "不发'继续'"的旧表述已删除；新增"收到'继续'怎么做"一节，写清从断点继续、不重新开始 |
| DOC2 | 自 | — | 比对 `VERSION.json` | `version=0.6.0`；`references/container-spec.md` 的默认模式为受管交互 |
| DOC3 | 自 | — | 检查 `annotation-spec.md` | 第 10 行"无故中断的数据重新 roll"已改为指向 R3–R4 |
| REL1 | 自 | — | 执行 `release.sh` | 两处技能目录的 `VERSION.json` 与 `scripts/` 哈希一致；幂等 |

---

## 4. P2 用例（工作台）

### 4.1 迁移（MG）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| MG1 | 自 | v3 数据库（含 20 道题） | 升级启动 | `schema_version` 增第 4 版；`session_rounds` 存在；旧列不丢；任务数不变 |
| MG2 | 自 | 已迁移 | 再启动一次 | 不重复迁移，不报错 |
| MG3 | 自 | 迁移中途 `kill -9` | 重启 | 迁移可重入；数据一致；记 `migration.recovered` |
| MG4 | 自 | 含 `sessionRun` 的 `state.json` | 加载 | 模型解析成功；缺 `sessionRun` 的旧题仍正常解析（默认值） |

### 4.2 网关（GW）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| GW1 | 自 | — | 检查命令白名单 | 含 `run-status`、`resume`、`continue`、`abort`；不含 `config key`（防泄露） |
| GW2 | 自 | 技能返回退出码 2 | 调网关 | 解析 stdout JSON 为结构化错误；1 则展示 stderr 原文；两者界面提示不同 |

### 4.3 执行器（EX）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| EX1 | 自 | 调 `run` 返回成功 | 一拍 | 进入轮询；不保持长连接；5 秒一次 `run-status` |
| EX2 | 自 | 轮询到 `status=accepted` | 一拍 | 退出轮询，写 `session_rounds`，任务进入下一阶段 |
| EX3 | 自 | 轮询到 `aborted` | 一拍 | 隔离任务；`outcome=aborted`；`error_kind` 与技能原因一致 |
| EX4 | 自 | `run-status` 连续 3 次失败 | 一拍 | 调 `resume`；仍失败则标 `blocked`，不终止容器 |
| EX5 | 演 | 运行中观察 SSE | — | `session.round_end`、`session.continue_sent` 事件在 2 秒内到达前端日志 |

### 4.4 重启认领（RC）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RC1 | 真 | 会话运行中 | 重启工作台后端 | 重启后 60 秒内界面恢复显示该题运行中；不新建容器；次数不变 |
| RC2 | 真 | 会话运行中，监督器已被杀 | 重启后端 | 自动 `resume` 接回；界面显示"监督器已接回" |
| RC3 | 自 | 容器与监督器都不在 | 对账轮 | 标 `blocked(session_lost)`；不自动重跑 |

### 4.5 策略（PT）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| PT1 | 自 | `run.incomplete` 类别 | 读取策略 | `charge=false, max=0`；不再重试 |
| PT2 | 自 | `autopilot.run_attempts` 在配置里写 2 | 启动 | 读到后强制为 1 并告警一次 |
| PT3 | 自 | `run.start_failed` 连续 2 次 | 一拍 | 第 3 次不再启动；任务标 `start_failed` 可人工重排；**未**进工程冷却 |

### 4.6 失败分流（FL）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| FL1 | 自 | 会话内出现 429，最终 accepted | 一拍 | 任务**不**记为失败；只记会话事件；弹性仍下调 |
| FL2 | 自 | 会话内出现 401 并 `holding` | 一拍 | 任务标 `blocked`；全局 `breaker` 触发（沿用现规则）；容器保留 |

### 4.7 容量（CP）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| CP1 | 自 | 上限 3，2 运行 + 1 等待继续 | 一拍 | 占用 = 3；不再启动新题；容量页显示"占用 3（运行 2 / 等待 1）" |
| CP2 | 自 | 有题处于 `holding` | 一拍 | 计入占用；不被守护当卡死 |
| CP3 | 自 | 等待继续持续 10 分钟 | 弹性轮 | `stall` 计入；达到阈值后下调上限（与限流的容器消失区分） |

### 4.8 守护（GD / SM-W）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| GD1 | 自 | 会话 `waiting_continue` 且 25 分钟无新行 | 无进展守护轮 | **不**命中（由技能自己的计时负责） |
| GD2 | 自 | `sessionRun.deadline.totalAt` 已过但技能仍在 running | 超时守护轮 | 只告警，不终止（终止权在技能） |
| SM-W1 | 自 | `state.status=session_aborted` | 对账 | `job_status=aborted`；原因原文显示 |
| SM-W2 | 自 | `sessionRun.status=holding` | 对账 | `job_status=blocked`；展示"等待人工处理：鉴权" |

### 4.9 接口（API）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| API1 | 自 | 运行中任务 | `POST /run/resume` | 200；返回 `run-status`；重复调用幂等 |
| API2 | 自 | `used=2` 的任务 | `POST /run/continue` | 409；提示次数用尽；不发送 |
| API3 | 自 | 运行中任务 | `POST /run/abort` | 200；任务转 `aborted`；留存证据 |
| API4 | 自 | 含 2 轮的会话 | `GET /run/session` | 轮次、中断类别、继续次数与 `session_rounds` 一致 |
| API5 | 自 | 有数据 | `GET /dispatch/session-stats` | 继续率 = 有继续的接受数 ÷ 接受数；终止原因分布正确 |
| API6 | 自 | — | `npm run gen:api && git diff --exit-code src/api/schema.ts` | 无差异（类型由 schema 生成） |

### 4.10 界面（UI）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| UI1 | 自 | 运行中 | 打开任务详情 | 会话面板显示"第 2 轮 · 已继续 1/2"、两个按钮可用、"继续"按钮点了要二次确认 |
| UI2 | 自 | `holding` | 同上 | 面板黄色；显示原因与倒计时；主按钮"处理完继续" |
| UI3 | 自 | `aborted` | 同上 | 面板红色；显示原因与证据路径；主按钮"换题" |
| UI4 | 自 | 有 3 轮数据 | 同上 | 时间线按轮次展示，中断行可见等待时长 |
| UI5 | 自 | 队列有等待继续的题 | 调度页 | 容量卡显示分流；不显示"卡死" |
| UI6 | 自 | — | 设置页 | 有运行模式、继续上限、等待时限、无进展时限；无"尝试次数" |
| UI7 | 自 | 旧配置含 `attempts=3` | 打开设置页 | 提示"v4 起固定一次会话"；不可编辑 |
| UI8 | 自 | — | 前端 grep | `outline: none` 为 0；`transition/animation` 总数 ≤ 12 + 本次新增（≤ 3） |

### 4.11 终止后归还（RB）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| RB1 | 自 | 任务因继续用尽终止，且来自题源领取 | 一拍 | 自动归还领取；工程进冷却；记事件 |
| RB2 | 自 | 同 RB1 | 一拍 | 触发补位；新任务入队；原工程本次目标内不再领取 |
| RB3 | 演 | 正在跑的会话 | 切 `dispatch.engine=v2` | 工作台回到旧界面；技能侧会话照常跑完 |
| RB4 | 演 | 正在跑的会话 | 切 `claude.runMode=print` | 新会话走无头；在跑的会话不受影响 |

---

## 5. P3 用例

### 5.1 端到端（E2E）

四个任务并发：

| # | 场景 | 期望 |
|---|---|---|
| E2E1 | 正常完成，0 次继续 | 接受；备注为空；导出 19 列齐全；`status` 十一项全绿 |
| E2E2 | 中途一次 429，自动继续 1 次后完成 | 接受；备注写 1 次；轮次表 2 行；评分 5 分被允许（限流不扣分） |
| E2E3 | 两次中断后用尽继续 | `aborted`；`终止说明.md` 存在；未导出；工程冷却；队列补位 |
| E2E4 | 人工 `abort` | 同上，原因为人工；不触发冷却以外的自动动作 |
| E2E5 | 四个任务的 SessionID、PromptID 各不相同 | 库层面无重复；导出无重复行 |
| E2E6 | 全程观察容量 | 占用数 = 运行 + 等待 + 保留；任一时刻不超过上限；容量时间线无空洞 |

### 5.2 故障注入（FI）

| # | 注入 | 期望 |
|---|---|---|
| FI1 | `kill -9` 监督器 | 60 秒内自动接回或可 `resume`；次数不变；无容器重启 |
| FI2 | 杀工作台后端并重启 | 会话不受影响；重启后 60 秒内恢复显示；不重复启动 |
| FI3 | 断网 60 秒 | 出现 `gateway` 类中断；恢复后自动继续；`used` 只增 1 |
| FI4 | `docker` 服务重启 | 容器丢失 → `session_lost` → 按策略终止或人工；不误报为模型问题 |
| FI5 | 磁盘可用 < 阈值 | 新的 `run` 被门禁拒绝；在跑的会话继续；界面显示磁盘挂起 |

### 5.3 试水（PW）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| PW1 | 真 | P1–P2 全部通过 | 提交 3–5 条带"继续"的数据 | 记录质检结果（通过/打回及原文）到 `docs/v4/evidence/qc-trial.md`；据此回填 `01` Q1；若被打回，按 `03` §4.2 回退到 `continueMax=0` |

### 5.4 长稳（LB）

| # | 类 | 前置 | 操作 | 期望 |
|---|---|---|---|---|
| LB1 | 演 | 隔离环境，8 小时连续调度 | 记录指标 | 无内存增长（RSS 波动 < 20%）；监督器 CPU < 单核 5%；无悬空 `running` |
| LB2 | 演 | 同 LB1 | 对比 `baseline.json` | 会话成功率、每容器小时产出、作废容器时长占比三项均达标（`01` §1）；未达标需说明原因并给出下一轮动作 |

---

## 6. 完成定义

以下全部满足才算完成：

1. P0–P3 全部用例通过；**真**类用例有运行记录（时间、任务目录、截图或日志路径）。
2. 技能 `0.6.0` 已发布到两处副本，`VERSION.json` 一致。
3. 工作台 `make check` 全绿，v3 既有测试无回归。
4. `docs/v4/implementation-status.md` 写明：已实现项、保守降级项、未覆盖的用例及原因。
5. `01` §6 的 Q1–Q3 有明确结论并回填本文档。
6. 回退开关（`claude.runMode`、`claude.continueMax`、`dispatch.engine`）实测可用。
