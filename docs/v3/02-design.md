# v3 · 02 · 设计说明（架构 / 内核 / 数据 / 接口 / 界面）

## 1. 架构

### 1.1 总览

```
                         ┌────────────────────── 调度页 / 顶栏徽标 / 命令面板 ──────────────────────┐
                         │  开关 · 上限 · 预设 · 目标 · 守护模式 · 队列操作        ▲ SSE dispatch.*  │
                         └───────────────┬───────────────────────────────────────┴──────────────────┘
                                         │ /api/dispatch/*
┌────────────────────────────────────────▼─────────────────────────────────────────────────────────┐
│                                      Dispatcher（调度内核）                                        │
│                                                                                                   │
│   运行意图 desired_state ──┐                                                                       │
│   挂起表 HoldRegistry ─────┼──▶ 准入门禁链 Gate Chain ──▶ 启动一个阶段 / 写等待原因                  │
│   资源池 Pools ────────────┤          ▲                                                           │
│   落盘队列 dispatch_items ─┘          │ 事实快照（每拍一份，只读）                                   │
│                                       │                                                           │
│   失败分类器 Classifier ──▶ 恢复策略 Policy ──▶ 重试 / 退避 / 隔离 / 挂起 / 转人工                    │
└───────┬───────────────┬───────────────┬───────────────┬───────────────┬───────────────┬──────────┘
        │               │               │               │               │               │
   阶段执行器        守护 Guards      探测 Probes     补位 Refill     看门狗 Watchdog   意图日志 Intents
 （沿用 v2 实现）  无进展/超时/      网关/Docker/    多项目加权      循环心跳/         领取/建仓/
  Autopilot 的     僵尸/孤儿/残留/    磁盘/题源/      背压/优先       崩溃循环/         导出/提交
  起草·运行·       领取泄漏          会话/录屏权限                   睡眠唤醒
  交付·导出
        │               │               │               │
        ▼               ▼               ▼               ▼
   技能 CLI（solo2611 run / collect / cleanup / record / export …）   Docker   工程台   SOLO2   ChatGPT 桌面
```

核心变化只有一句话：**v2 的三个循环各自决定「现在能不能干活」，v3 把这个决定收到一处**，
执行器只负责「怎么干」。`Autopilot` 里按磁盘事实推导阶段的逻辑（`_advance`）原样保留——那是 v2 最有价值的部分。

### 1.2 循环划分

参考项目踩过的坑：启动门禁和自动补队放在同一个循环里，补队在登录与分页请求上花几分钟，
这段时间门禁完全不跑，磁盘已经腾出来了队列却不动（其 v0.4.4 修复）。v3 从一开始就按「快慢」拆开：

| 循环 | 周期 | 只做什么 | 允许的阻塞 |
|---|---|---|---|
| `gate` | 2 s | 读事实快照 → 跑门禁链 → 至多启动一个容器阶段和一个会话阶段 → 写等待原因 | 无。只读内存与一次带超时的 `docker ps` 结果缓存 |
| `facts` | 2 s | 采集 Docker 容器列表、磁盘、进程表，产出事实快照；失败不覆盖为「空」而是标记为「未知」 | `docker ps` 超时（默认 20 s，单飞） |
| `advance` | 3 s | 对每道活跃题按磁盘事实推导下一步（v2 `_advance`），向 `gate` 提出阶段申请 | 读文件 |
| `probe` | 各探测器自定 | 网关 / Docker / 磁盘 / 题源 / 会话 / 录屏权限探测，结果写挂起表 | 网络请求，各自超时 |
| `refill` | 60 s | 领取 → 下载 → 建题 → 入队 | 分钟级 |
| `guard` | 60 s | 守护检查与（若开启）处置 | 秒级 |
| `reconcile` | 60 s | v2 对账（磁盘 → 索引）+ v3 意图收敛 | 秒级 |
| `elastic` | 30 s | 扫描 429、调整生效上限 | 读文件尾部 |
| `janitor` | 30 min | v2 回收站 / 瘦身 + 磁盘紧张时的紧急瘦身 | 秒到分钟 |
| `watchdog` | 30 s | 检查以上所有循环的心跳 | 无 |

所有循环经 `LoopHealth` 注册，统一 `begin / end` 上报心跳（§5.4）。

### 1.3 模块布局

```
backend/solo_auto/dispatch/           ← 新增包
  kernel.py        Dispatcher：运行意图、gate 循环、阶段申请与发放
  facts.py         FactsSnapshot：容器、磁盘、进程表、时间基准；Docker 单飞与「未知」语义
  pools.py         ResourcePool、容量账本、预约与幽灵占用
  gates.py         门禁链：每道门一个纯函数 (snapshot, item) -> Pass | Wait(code, text, until)
  holds.py         HoldRegistry：挂起的增删、范围、探测调度、落盘
  queue.py         dispatch_items 的读写与排序
  classify.py      失败分类器 + patterns 加载
  policy.py        恢复策略表：类别 → 重试 / 退避 / 范围 / 去向
  elastic.py       弹性容器上限
  intents.py       副作用意图日志
  recovery.py      启动认领、故障后清理残留
  throughput.py    outcomes 记录与统计
  watchdog.py      LoopHealth + 自退出
  clock.py         单调 / 挂钟双时钟、睡眠检测、可注入的虚拟时钟
  faults.py        故障注入点（仅 SOLO_AUTO_FAULTS 置位时生效）
  probes/          llm.py docker.py disk.py source.py session.py recording.py skill.py
  guards/          progress.py timeout.py zombie.py orphan.py leak.py claims.py inflation.py
  refill.py        由 pipeline/intake.py 演进而来
backend/solo_auto/api/dispatch.py     ← 新增路由
backend/solo_auto/pipeline/
  scheduler.py     RunScheduler → RunExecutor：去掉自己的 tick 与队列，只保留「执行一次 run 并流式回传」
  autopilot.py     保留阶段推导与执行；并发判断、失败计数、熔断调用改为委托 Dispatcher
  intake.py        逻辑迁入 dispatch/refill.py，本文件保留兼容外壳一个版本
  breaker.py       保留计数逻辑；trip() 改为向 HoldRegistry 登记一条 breaker 挂起
frontend/src/pages/DispatchPage.tsx           ← 新增，取代 ProductionPage
frontend/src/pages/DispatchLogPage.tsx        ← 新增
frontend/src/components/dispatch/*            ← 上限面板、挂起条、守护面板、队列、吞吐、解释器
```

## 2. 关键取舍（ADR，接 v2 编号）

### ADR-011 · 调度参数与接线配置分家

**决定**：`config.toml` 新增 `[dispatch]` 表承载全部调度参数，由调度页编辑；设置页只留接线类配置。
**理由**：调度参数的特点是「看着现场调」，接线配置的特点是「配一次不动」，放在一起两边都难用。
**代价**：一次配置迁移（§6.3）；设置页上原有的并发字段要留一个「已移到调度页」的指引。

### ADR-012 · 挂起模型取代一刀切熔断

**决定**：所有自动停机都表达为带来源、范围、探测器的「挂起」；调度是否可跑 = 人工意图为运行 **且** 相关范围内没有挂起。
**理由**：v2 熔断把「需要人判断的质量问题」和「等一会儿就好的基础设施抖动」混成一件事，后者占绝大多数。
参考项目的 `llmGuard.pausedByGuard` 已经证明「只解除自己挂的暂停」这一条规则足够安全，这里把它推广。
**代价**：状态从一个布尔变成一张表；界面必须把表讲清楚（§7.2 挂起条）。

### ADR-013 · 平台决定何时启动，技能上限只作硬顶

**决定**：容器准入完全由平台判断；写回技能的 `claude.maxContainers` = 平台的**弹性上限**（未开弹性时 = 容器数）。
平台的生效上限在 [容器数, 弹性上限] 之间浮动，永远不超过技能值。
**理由**：2611 是一题一容器，平台每调用一次 `run` 就是一个容器，平台自己数得清；不需要像参考项目那样依赖技能侧限流器排队
（参考项目一题两个候选，才需要技能侧按号排队）。这样弹性伸缩不要求技能支持热读上限。
**代价**：如果用户绕过平台手动跑 `solo2611 run`，平台只能通过容器标签看到并计入占用，无法阻止——这是可接受的，且会如实显示。

### ADR-014 · 不合并三种调度模式，改为预设

**决定**：不照搬参考项目的「容器优先 / 任务数优先」双模式，提供 跑满 / 均衡 / 稳妥 三档预设 + 自定义。
**理由**：参考项目里一道题占两个容器且只在候选赛阶段占用，两种模式的差别很大；这里一道题一个容器，
差别退化为「在手题目数相对容器数的倍数」，用一个派生参数就能表达。
**代价**：无。预设只是三组派生参数（§3.4）。

### ADR-015 · 守护默认仅观察

**决定**：所有会删容器、杀进程、归还领取的守护默认运行在「仅观察」，由用户在看过命中记录后手动切到「自动处置」。
**理由**：参考项目 v0.6.0 的任务租约把「在排队等容器的任务」误判成无活动，扣回额度并判失败，导致多开任务，最后整版撤回。
误杀的代价远高于晚杀。
**代价**：首次上线的前几天需要人看一眼命中面板。

### ADR-016 · 运行意图跨重启保留，崩溃循环例外

**决定**：`desired_state` 落盘，重启后沿用；仅当 15 分钟内异常退出 ≥ 2 次时本次启动挂起。
**理由**：无人值守场景下，「重启后默认暂停」等于每次崩溃都要人来点一下；但真正的崩溃循环又必须停。
**代价**：需要区分正常退出与异常退出（`starts.json`，§5.5）。

### ADR-017 · 不消耗重试次数的「归因失败」

**决定**：由基础设施造成的失败（网关断、Docker 挂、磁盘满、后端重启）不计入题目自己的重试次数，但计入单题总尝试上限。
**理由**：否则一次 20 分钟的网关故障会把在跑的每道题的重试次数全部耗尽，恢复后全是「已隔离」。
**代价**：分类必须可靠——分类不出来时按 `unknown` 处理并计入次数（保守）。

## 3. 调度内核

### 3.1 运行状态

用户能设置的只有**运行意图**；界面显示的是意图与挂起叠加后的**有效状态**。

```
运行意图 desired_state（落盘，用户或命令面板修改）
  running   正常调度
  paused    不启动任何新阶段；在跑的阶段跑完
  draining  不启动新题、不领取；已在手的题继续走完全部阶段，走完后自动转 paused

有效状态 effective_state（只读，每拍计算）
  running          意图 running 且全局范围无挂起
  degraded         意图 running，有非全局挂起（例如只有领取被挂起）——仍在干活，但不完整
  held             意图 running，但全局或容器池被挂起——没有新阶段能启动
  paused           意图 paused
  draining         意图 draining 且仍有在手的题
  idle             意图 running、无挂起、但没有可做的事（队列空且无目标）
```

「立即中断」不是一种状态，而是一个动作：把意图置为 `paused`，并对选中的（或全部）在跑阶段执行 v2 已有的停止流程
（杀 Agent、可选清容器）。被中断的题进入 `interrupted`，下次启动时按磁盘事实续跑，不计失败。

### 3.2 资源池

| 池 | 占用者 | 上限来源 | 占用如何计算 |
|---|---|---|---|
| `containers` | 处于运行阶段的题 | 生效上限（§3.5） | 见下 |
| `sessions` | ChatGPT 模式：从起草到交付全程；CLI 模式不使用 | `dispatch.sessions`（0 = 自动 = 在手上限） | 平台登记的活跃会话数 |
| `authoring` / `delivering` | CLI 模式下各自的 Agent 进程 | `dispatch.authoring` / `dispatch.delivering` | 活跃 Agent 进程数 |
| `recording` | 正在录屏的题 | `dispatch.recording`（默认 1） | 录屏服务状态 |
| `exporting` | 正在导出 / 提交的题 | 固定 1 | — |
| `in_hand`（准池） | 已领取未终态的题 | `dispatch.in_hand`（0 = 自适应，§3.4） | 队列中非终态且非隔离的项数 |

**容器占用的唯一口径**（FR-CAP-02）：

```
live       = 事实快照中带 solo2611 标签的运行中容器名集合
reserved   = 已由平台启动 run、但 live 里还没出现对应容器的任务集合
foreign    = live 中不属于任何平台已知任务的容器（用户手动跑的、别的实例的）
occupied   = |live 对应的任务 ∪ reserved| + |foreign|
```

- 快照里 Docker 状态为 `unknown`（本拍与上一拍都没拿到成功结果，或结果已超过 `docker_stale_seconds`）时，
  `containers` 池整体不可发放，等待原因为 `docker.unknown`。**绝不**回退到「只数内存里的活跃数」。
- 展示可以用旧快照（标注「Docker 响应慢，显示 N 秒前的数据」），准入不可以。
- `reserved` 中的预约超过 `phantom_seconds`（默认 600）仍无容器：技能进程已退出 → 释放预约、该次尝试按
  `container.start_failed` 进入策略；技能进程仍活着 → 保留占用，只记一次 `capacity.starved`。

### 3.3 准入门禁链

每拍对「队首的可启动项」依次过门，第一道不过的门决定等待原因。门是纯函数，输入只有事实快照、配置与队列项，
因此可以在虚拟时钟下穷举测试。

**容器阶段（启动 `run`）**

| 序 | 门 | 不过时的原因码 | 说明文案示例 |
|---|---|---|---|
| G1 | 总开关与引擎 | `dispatch.disabled` | 调度总开关已关闭 |
| G2 | 运行意图 | `intent.paused` / `intent.draining` | 已手动暂停 |
| G3 | 定时窗口 | `window.closed` | 不在允许启动的时段，08:00 后继续 |
| G4 | 全局挂起 | `hold.<source>` | 已自动挂起：崩溃循环保护 |
| G5 | 启动宽限 | `startup.grace` | 刚重启，正在核对现场，约 14 秒后开始 |
| G6 | Docker 已知且健康 | `docker.unknown` / `hold.docker` | Docker 状态未确认，暂不启动 |
| G7 | 磁盘 | `hold.disk` | 磁盘剩余 27 GB，低于 30 GB：清出空间后自动继续 |
| G8 | 上限同步 | `limit.resync` | 技能侧上限刚被纠正，约 20 秒后启动 |
| G9 | 容器池挂起 | `hold.llm` 等 | 网关连续 2 次探测失败，4 分钟后复测 |
| G10 | 容器名额 | `capacity.full` / `capacity.draining` | 容器已满 4/4 ；收敛中 5/4 |
| G11 | 启动间隔 | `spacing` | 距上次启动 18 秒，间隔 45 秒 |
| G12 | 项自身 | `backoff` / `quarantined` / `duplicate_engineering` / `not_ready` | 3 分钟后第 2 次重试 |

G1–G11 是「队列级」的门：不过时原因挂在队首项上，其余等待项显示「排队第 n 位」。
G12 是「项级」的门：不过时跳过该项看下一项，原因挂在该项自己身上。这样不会出现一道退避中的题堵住整条队列。

**会话阶段（拉起 ChatGPT 会话或 CLI Agent）**的门链相同，G6–G11 换成：会话池挂起（`hold.session`，例如 ChatGPT 未登录 /
用量受限）、会话名额、会话启动间隔（≥ 20 s，FR-CAP-05）、在手上限。

**启动间隔的自适应**（参考项目 v0.5.0 的经验）：空闲名额 ≥ 2 时只守最低间隔（10 s），只剩最后一个名额时按配置间隔，
避免「4 个容器空着 3 个，却每 45 秒才补一个」。

**等待原因的生命周期**：原因写在队列项的 `wait_code` / `wait_text` / `wait_until` 上；每拍重算；
项启动或条件消失时清空。不保留历史文案（历史在调度日志里）。

### 3.4 预设与在手上限推导

要让 C 个容器保持跑满，需要在手的题数取决于一道题在容器外停留多久：

```
在手需求 N* = ceil( C × (Ta + Tw + Tr + Td) / Tr )
  Ta 起草中位耗时   Tr 运行中位耗时   Td 交付+录屏+导出中位耗时   Tw 等容器平均耗时（目标为 0，取 0）
```

`Ta / Tr / Td` 取最近 20 道完成题的中位数（§5.6 的 `outcomes`）；样本不足 5 道时用保守默认 `N* = C + 2`。

| 预设 | 在手上限 | 会话并发 | 启动间隔 | 适用 |
|---|---|---|---|---|
| 跑满 `throughput` | `clamp(N*, C+1, 3C)` | = 在手上限 | 30 s | Key 并发是瓶颈，想让容器不空 |
| 均衡 `balanced`（默认） | `clamp(ceil((N* + C) / 2), C+1, 2C+1)` | = 在手上限 | 45 s | 日常 |
| 稳妥 `careful` | `C` | `C` | 90 s | 白天自己也要用机器；或新题型试跑 |
| 自定义 `custom` | 手填 | 手填 | 手填 | — |

派生值每 10 分钟重算一次，变化时记 `limit.derived`。界面上派生字段显示为灰底并标注「自动」，点一下可转为手填（预设随之变为自定义）。

硬约束：`在手上限 ≥ C`、`会话并发 ≤ 8`、`recording ≥ 1`。背压（FR-RFL-05）生效时在手上限临时取 `min(在手上限, 当前在手数)`，
即只出不进。

### 3.5 弹性容器上限

沿用参考项目经过线上两轮修正的规则（其 v0.5.0）：**按 429 实际耽误的时间判断，而不是数 429 的条数**
——它的实测是两天 153 次 429 里七成半秒内重试就过，合计只多等 6 分钟。

```
配置：containers = 保底 F；elastic.ceiling = 上限 H（≤ 8）；生效上限 L ∈ [F, H]

信号（每 30 s 扫描各运行中尝试的 stdout.jsonl 尾部增量）：
  stall     = 近 window(300 s) 内 429 重试等待时长之和 ÷ (L × window)
  severe    = 是否有单个请求重试到第 severe_attempt(6) 次

下调：stall > stall_percent(3%) 或 severe → L -= 1（不低于 F），两次下调至少间隔 180 s
上调：容器全满 且 有题在等容器 且 stall < stall_percent/2 且 距上次下调 > cooldown(600 s)
      → 每 step_up(300 s) L += 1（不高于 H）
记忆：某个 L 全满且无压力保持 stable(1800 s) → 记为「本小时的稳定值」（按一天 24 小时分桶，保留 7 天）；
      下调后的值也记。进入同一小时、切换开关、修改保底时直接从记忆值开始，而不是从 F 一格格爬。
到底：已在 F 仍然 stall 超阈 → 不再降，记 elastic.rate_limited_at_floor 并触发一次网关探测（可能是网关侧问题）
```

状态落盘于 `run/elastic.json`。事件：`elastic.up` / `elastic.down` / `elastic.resume` / `elastic.rate_limited_at_floor`。

手动修改保底时：从新保底开始，至少保持一个升档间隔再考虑上调（避免刚改完就跳到记忆值）。

> 前提待确认（Q-V3-1）：solo-2611 容器内 Claude Code 的 `stdout.jsonl` 是否与参考项目一样输出带 `retry_delay_ms` 的
> `api_retry` 事件。若不是，M14 的第一个任务是改为从轨迹里可得的等价信号计算；信号拿不到时弹性保持关闭并在界面说明。

### 3.6 队列排序

```
排序键 = (隔离置后, 置顶优先, 优先项目优先, 续跑中的题优先于新题, not_before, enqueued_at)
```

- 「续跑中的题优先于新题」：已经花了成本的题先拿名额，减少在制品。
- 退避中的项不参与本拍选择但保留位置。
- 「接下来启动」预览直接取本排序的前 10 项，并用启动间隔与当前空位估算时间；预览与实际共用同一个函数，不允许两套逻辑。

### 3.7 失败处理流水

```
阶段结束（成功 / 失败 / 被守护终止 / 被中断）
   │
   ▼
Classifier.classify(证据) ──▶ Failure{kind, transient, scope, charge, detail, evidence}
   │
   ▼
Policy.decide(item, failure) ──▶ 动作集合：
      retry(after)        退避后重试本阶段
      requeue(no_charge)  归因失败，不计次数，回队列
      quarantine          隔离本题，补位新题
      cooldown(engineering) 工程进冷却名单
      hold(scope, source) 登记挂起并启动对应探测
      probe(source)       立即加测
      escalate            进收件箱 + 通知
   │
   ▼
写事件（含分类依据）→ 更新队列项 → SSE
```

分类器与策略表的完整内容见 `03-failure-playbook.md` §2、§3。

## 4. 挂起模型

### 4.1 数据结构

```jsonc
// run/holds.json
{
  "holds": [
    {
      "id": "llm",                    // 每个来源至多一条
      "source": "llm_guard",          // llm_guard | docker_guard | disk_guard | source_guard | session_guard |
                                      // recording_guard | skill_guard | breaker | crash_loop |
                                      // unknown_burst | window | submit_guard
      "scope": ["containers"],        // global | intake | containers | sessions | recording | submit | task:<id>
      "reason_code": "gateway.unreachable",
      "reason": "网关连续 2 次探测失败（超时 30 s）",
      "since": "2026-11-12T02:14:07+08:00",
      "auto": true,                   // false = 只能人工解除
      "probe": {"next_at": "…", "count": 3, "last_error": "timeout"},
      "self_heal_deadline": "…",      // 超过后升级通知
      "caused_by_task": "…"           // 可空：触发它的那道题
    }
  ],
  "history": [ /* 最近 200 条已解除的挂起，含持续时长 */ ]
}
```

### 4.2 规则

1. 每个来源只有自己能登记和解除自己的挂起（硬规矩 9 的实现点）。人工「手动解除」是唯一的例外入口，
   解除后来源若仍判定异常，会在下一次探测时重新挂起——不提供「永久忽略」，要忽略就去关掉那个守护。
2. 范围决定影响面：门禁链按阶段类型只查相关范围。`global` 影响一切新启动；`task:<id>` 只影响一道题。
3. `auto=false` 的挂起（`breaker`、`crash_loop`、鉴权类）没有探测器，等人。
4. 解除时调用 `recovery.after_hold(hold)`：处理挂起期间的残留（FR-HLD-09）。
5. 挂起不影响在跑的阶段，除非该来源的策略显式要求（目前只有磁盘「紧急」档会中断最新启动的一个运行来保住其余的）。

### 4.3 来源一览

| 来源 | 范围 | 挂起条件 | 探测 / 恢复 | 最长自愈时间 |
|---|---|---|---|---|
| `llm_guard` | containers | 连续 `fail_threshold`(2) 次一 token 探测失败 | 挂起期间每 5 分钟；成功 1 次即解除 | 60 分钟 |
| `docker_guard` | containers, recording | 连续 3 次 `docker ps` 失败（含超时） | 每 30 s；连续 2 次成功解除 | 15 分钟 |
| `disk_guard` | containers, intake | 剩余 < 下限 | 每 60 s；≥ 下限 × 1.2 解除；挂起时触发紧急瘦身 | 30 分钟 |
| `source_guard` | intake | 重登失败 / 连续 3 次 5xx 或超时 / 所有项目无候选 | 指数退避 1→30 分钟 | 120 分钟（无候选不升级，只在状态句说明） |
| `session_guard` | sessions | ChatGPT 未运行 / 未登录 / 用量受限 / 辅助功能权限丢失 / CLI Agent 不可执行 | 每 2 分钟；用量受限按提示的重置时间 | 30 分钟 |
| `recording_guard` | recording | 屏幕录制权限丢失 / 录屏服务连续失败 2 次 | 每 5 分钟 | 15 分钟后升级（权限只能人给） |
| `skill_guard` | global | 技能版本变化 / 技能脚本不可执行 / 表头导入失败 | 每 60 s | 立即升级 |
| `submit_guard` | submit | SOLO2 登录失败 / 连续提交失败 | 每 5 分钟 | 60 分钟 |
| `unknown_burst` | intake | 最近 10 题中 `unknown` ≥ 4 | 无（人工） | 立即升级 |
| `breaker` | global | v2 规则：同类连续失败、抽检打回率、鉴权失败 | 无（人工） | 立即升级 |
| `crash_loop` | global | 15 分钟内异常退出 ≥ 2 次 | 无（人工） | 立即升级 |
| `window` | containers, sessions, intake | 不在定时窗口 | 到点自动解除 | 不升级 |

### 4.4 网关探测细节

- 复用 v2 `Preflight.probe_api()` 的一 token 请求；每次探测占一个 Key 并发，所以健康时默认 30 分钟一次。
- 首次失败后 `retry_seconds`(30) 即复测，而不是等完整周期——断连约 1 分钟内挂起。
- **即时触发**：任一运行以 `gateway.*` / `network.*` 类失败结束、或弹性在保底仍被限流、或提交质检接口连续失败，
  都立即加测一次；同一段连续失败期间只触发一次。
- 探测判定「可用」的标准：HTTP 成功且回应里的模型名与技能配置一致（参考项目 v1.4.0 的做法：不只看有没有回复）。
  模型名不一致记为 `gateway.model_mismatch`，按 `auth` 同级处理（需人工）。
- 401 / 403 不属于「断连」：直接登记 `breaker` 挂起（`auth`），不做自动恢复。

## 5. 可靠性

### 5.1 落盘与一致性

| 数据 | 存放 | 写入方式 |
|---|---|---|
| 队列项、意图日志、outcomes、调度事件 | SQLite（WAL） | 事务；队列项状态变更与对应事件同一事务 |
| 运行意图、挂起表、弹性状态、启动记录 | `run/*.json` | 临时文件 + `os.replace`；写前把旧文件复制为 `.bak` |
| 槽位文件 | `run/slots/<task>.json` | 同上；v3 起写入真实 `pid` 与 `pgid` |

读失败（JSON 损坏、半截文件）时：先读 `.bak`；仍失败则以空状态启动并从磁盘事实重建（队列从任务目录与 `state.json` 重建，
挂起表置空后由探测器在首轮重新得出），同时登记一条升级事件。**任何状态文件损坏都不得导致后端拒绝启动。**

### 5.2 重启认领（FR-RCV-02）

重启后在启动宽限期内，对每个「记录在案的运行」（槽位文件 ∪ 队列中 `launched` 的项 ∪ `state.json` 为 `running` 的任务）：

| 技能进程 | 容器 | 判断 | 处理 |
|---|---|---|---|
| 活 | 活 | 上次的运行还在正常进行 | **认领**：登记占用，启动一个跟随器读取该次尝试的日志文件，等技能进程退出后按正常结束流程处理。不重启、不计次数 |
| 活 | 无 | 技能在排队 / 启动中，或刚结束在收尾 | 认领并等待；超过 `phantom_seconds` 仍无容器且无写入 → 终止该技能进程，按 `container.start_failed` |
| 死 | 活 | 技能进程被杀（例如随旧后端一起被系统回收），容器还在跑 | 登记占用；容器结束后调用技能 `collect --container` 收尾；容器超过运行超时 → 技能 `cleanup` 后按 `run.timeout` |
| 死 | 无 | 运行已结束或已崩溃，现场没收 | 调用技能 `collect`：成功 → 正常进入下一阶段；失败 → 按 `run.interrupted`（归因失败，不计次数）回队列 |

认领依据 `pid` 时必须同时核对进程命令行包含该任务目录，防止 PID 复用导致误认。

Agent 会话（CLI 模式）的处理与 v2 相同但不再无条件 `SIGKILL`：进程仍活着且日志在增长 → 认领并重新挂上日志跟随；
否则终止并回到由磁盘事实推导。ChatGPT 桌面会话本来就在后端之外，按 v2 的 marker + rollout 定位重新跟随。

### 5.3 副作用意图日志（FR-RCV-04）

```
dispatch_intents(id, kind, key, payload, state, created_at, resolved_at, result)
  kind  = claim | github_init | export | submit | release
  key   = 幂等键：claim → project:engineering；github_init → task；export → task:batch；submit → task
  state = pending | done | failed | abandoned
```

流程：写 `pending` → 执行副作用 → 写 `done / failed`。重启后对所有 `pending`：

| kind | 外部事实怎么查 | 收敛动作 |
|---|---|---|
| `claim` | 工程台「我的领取」里是否有该工程 | 有且本地无任务 → 接着下载建题；有且本地有任务 → 标 done；没有 → 标 failed |
| `github_init` | `state.json` 的 `remote.initialPushed` 与远端仓库是否存在 | 已推送 → done；仓库存在但未推送 → 交回技能重试（技能自身幂等）；不存在 → failed |
| `export` | 导出目录里的文件与任务的导出记录 | 文件齐全 → done；否则 failed 后由 `_advance` 重新导出 |
| `submit` | SOLO2 提交记录 | 已有记录 → done，**绝不重提**；查询失败 → 保持 pending，挂 `submit_guard` |
| `release` | 工程台是否仍显示已领取 | 仍领取 → 重试归还；已归还 → done |

同一幂等键存在 `pending` 意图时，任何路径都不得发起第二次相同副作用。

### 5.4 看门狗（FR-RCV-05 / 06）

```python
health.register("gate", interval=2, stall_after=60)      # 默认 max(10 × interval, 60 s)
health.register("refill", interval=60, stall_after=900)  # 慢循环放宽
async with health.beat("gate"): await self._tick()
```

- `stalled`：超过 `stall_after` 没完成一轮。首次进入时记 `watchdog.loop_stalled`，附该 asyncio 任务的当前栈
  （`task.get_stack()`）。
- `dead`：任务已结束（异常或被取消但进程未在关闭）。自动重建并记 `watchdog.loop_dead`。
- `/api/health`：`ok = 没有 stalled 的循环`；附每个循环的 上次心跳 / 状态 / 连续错误数。
- **自退出**：`gate` 或 `facts` 连续 stalled 超过 `exit_after_seconds`(300) → 记事件、落盘、`os._exit(70)`，
  由 launchd `KeepAlive` 拉起。慢循环 stalled 只重建不退出。
- asyncio 事件循环本身被同步调用卡死时，上述检查也跑不了。因此另起一个**独立线程**的心跳检查：
  事件循环每秒更新一个时间戳，线程发现超过 120 秒未更新 → 直接 `os._exit(70)`。
- `scripts/launchd` 的 plist 增加 `ThrottleInterval`（10 s）防止拉起风暴；崩溃循环由 §5.5 兜住。

### 5.5 启动记录与崩溃循环

`run/starts.json` 保存最近 20 次启动：`{started_at, clean_exit, pid, version}`。正常关闭（走到 `shutdown()`）把上一条的
`clean_exit` 置真。启动时统计 15 分钟内 `clean_exit=false` 的条数：≥ 2 → 登记 `crash_loop` 挂起并通知；否则沿用
`desired_state`，记 `dispatch.resumed_on_start`。

用户主动「重启后端」（v2 FR-PLAT-03）走正常关闭，不计入。

### 5.6 时钟与睡眠

- 所有「经过了多久」用单调时钟；所有展示与落盘用挂钟。`clock.py` 提供可注入的时钟接口，测试用虚拟时钟。
- 每拍比较 `挂钟增量 − 单调增量`（macOS 上单调时钟在睡眠期间不走）：差值 > 30 s 判定为一次睡眠，记 `system.wake`，
  把差值累加到 `slept_seconds`；守护的无进展 / 阶段预算 / 幽灵占用判断统一减去区间内的睡眠时长。
- 唤醒后立即：强制全部探测器跑一轮、Docker 事实刷新、题源登录态检查。
- 调度有效状态为 `running / degraded / draining` 时持有一个 `caffeinate -i -w <后端 pid>` 子进程；
  转 `paused / held(人工类) / idle` 超过 5 分钟后释放。合盖睡眠 `caffeinate -i` 挡不住，靠上面的唤醒处理兜底。

### 5.7 故障后清理残留（FR-HLD-09）

`recovery.after_hold(hold)` 与手动入口 `POST /api/dispatch/recover {since_minutes}` 共用：

1. 找出 `since` 之后因 `hold.source` 对应失败族而失败 / 隔离的队列项。
2. 对每一项：其容器若仍在且属于已放弃的尝试 → 技能 `cleanup`；把项状态改回 `queued`，
   `charged_attempts` 回退到故障前的值，`not_before` 置为现在 + 随机 0–60 秒（避免恢复瞬间齐射）。
3. 人工操作过的项（手动隔离、手动移出）不动；仍有存活容器且在正常跑的项不动。
4. 汇总为一条 `recovery.done` 事件：移除容器 n、重新排队 n、保留 n。

## 6. 数据与配置

### 6.1 SQLite 增量

```sql
CREATE TABLE IF NOT EXISTS dispatch_items (
  task_id          TEXT PRIMARY KEY,
  status           TEXT NOT NULL,   -- queued | launching | active | backoff | interrupted | quarantined | done | failed | removed
  stage            TEXT NOT NULL,   -- 最近一次由 _advance 推导出的阶段
  priority         INTEGER NOT NULL DEFAULT 0,   -- 置顶 / 优先项目
  position         REAL NOT NULL,                -- 手动排序用
  enqueued_at      TEXT NOT NULL,
  not_before       TEXT,                         -- 退避到期时间
  wait_code        TEXT, wait_text TEXT, wait_until TEXT,
  attempts         TEXT NOT NULL DEFAULT '{}',   -- {stage: 计费次数}
  total_attempts   INTEGER NOT NULL DEFAULT 0,   -- 含不计费的
  last_failure     TEXT,                         -- JSON：kind / detail / evidence / at
  repeat_signature TEXT,                         -- 上次门禁 errors 的指纹，用于「重复同一错误」止损
  guard_flags      TEXT NOT NULL DEFAULT '[]',
  keep             INTEGER NOT NULL DEFAULT 0,   -- 用户标记「保留，守护不处理」
  plan_id          TEXT, project_id TEXT, engineering_id TEXT,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_intents ( … 见 §5.3 … );

CREATE TABLE IF NOT EXISTS dispatch_outcomes (
  task_id TEXT, finished_at TEXT, outcome TEXT,          -- done | quarantined | failed | removed
  queued_s REAL, authoring_s REAL, wait_container_s REAL, running_s REAL,
  delivering_s REAL, recording_s REAL, exporting_s REAL, total_s REAL,
  attempts INTEGER, failure_kind TEXT, project_id TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_capacity (            -- 容量时间线，每分钟一行，保留 7 天
  at TEXT PRIMARY KEY, occupied INTEGER, limit_effective INTEGER, limit_floor INTEGER,
  in_hand INTEGER, sessions INTEGER, held TEXT
);

CREATE TABLE IF NOT EXISTS engineering_cooldown (
  engineering_id TEXT PRIMARY KEY, project_id TEXT, until TEXT, reason TEXT, failures INTEGER
);
```

调度事件继续写 v2 的 `events` 表，`kind` 使用下列族前缀：
`dispatch.*`（意图与参数变更）、`gate.*`（等待原因首次出现 / 消失）、`hold.*`、`probe.*`、`retry.*`、`guard.*`、
`elastic.*`、`capacity.*`、`limit.*`、`recovery.*`、`watchdog.*`、`refill.*`、`intent.*`。
同一状况只记一次（进入时一条、解除时一条），不允许每拍刷一条。

`schema_version` 递增；迁移脚本从 v2 的 autopilot 状态 JSON 与生产计划 JSON 生成 `dispatch_items`，原文件保留不删。

### 6.2 磁盘布局增量（`~/solo-auto/`）

```
run/dispatch.json     运行意图、预设、上次参数变更
run/holds.json        挂起表与历史
run/elastic.json      弹性状态与按小时记忆
run/starts.json       启动记录
run/*.json.bak        上述文件的上一份
dispatch/patterns.toml  失败特征表（首次启动从包内默认复制；用户可编辑）
logs/dispatch.jsonl   调度事件的 JSONL 镜像（20 MB 轮转，供 tail 与诊断包）
```

### 6.3 配置（`config.toml` 新增表）

```toml
[dispatch]
engine = "v3"                 # v3 | v2（整套回退）
enabled = true                # 总开关，承接 autopilot.enabled
preset = "balanced"           # throughput | balanced | careful | custom
containers = 4                # 容器数（弹性开启时为保底），1–8
sessions = 0                  # 0 = 自动
authoring = 2                 # 仅 CLI 模式
delivering = 1                # 仅 CLI 模式
recording = 1
in_hand = 0                   # 0 = 自适应
launch_spacing_seconds = 45   # 最低 10
session_spacing_seconds = 20  # 最低 20
startup_grace_seconds = 20
phantom_seconds = 600
docker_timeout_seconds = 20
docker_stale_seconds = 15
keep_awake = true

[dispatch.elastic]
enabled = false
ceiling = 6
window_seconds = 300
stall_percent = 3.0
severe_attempt = 6
step_up_seconds = 300
cooldown_seconds = 600
stable_seconds = 1800

[dispatch.retry]
max_total_attempts = 8        # 单题总尝试上限（含不计费）
backoff_seconds = [60, 180, 600]
jitter = 0.2
unknown_retries = 1
engineering_cooldown_failures = 2
engineering_cooldown_hours = 72
# 各类别的覆盖项见 patterns.toml / 调度页重试策略表

[dispatch.guard]
mode = "observe"              # off | observe | enforce
run_overtime_factor = 1.25
no_progress_minutes = 30
budget_authoring_minutes = 90
budget_delivering_minutes = 120
budget_recording_minutes = 20
budget_exporting_minutes = 15
zombie_grace_minutes = 5
orphan_container_minutes = 30
leaked_process_minutes = 60
claim_leak_minutes = 30
attempt_inflation = 50

[dispatch.llm_guard]
enabled = true
probe_seconds = 1800
retry_seconds = 30
paused_probe_seconds = 300
fail_threshold = 2
timeout_seconds = 30

[dispatch.disk]
floor_gb = 30.0               # 承接 storage.disk_floor_gb
resume_factor = 1.2
emergency_gb = 10.0           # 低于此值进入紧急档

[dispatch.refill]
mode = "target"               # target | continuous
target = 0
daily_claim_cap = 30
allow_reuse = false
engineering_usage_limit = 1
refill_while_paused = false
# 项目与权重存在 run/dispatch.json 的 plan 里，不放 config（它是操作态不是配置）

[dispatch.watchdog]
check_seconds = 30
exit_after_seconds = 300

[dispatch.window]
enabled = false
allow = ["00:00-08:00", "20:00-24:00"]
```

**迁移映射**（首次以 v3 启动时执行一次，原键保留只读一个版本，设置页显示「已移到调度页」）：

| v2 键 | v3 键 |
|---|---|
| `execution.max_containers` | `dispatch.containers` |
| `autopilot.enabled` | `dispatch.enabled` |
| `autopilot.parallel_authoring` / `parallel_delivering` | `dispatch.authoring` / `dispatch.delivering`；两者之和若 ≠ 默认推导值则写入 `dispatch.sessions` 并置 `preset=custom` |
| `autopilot.stage_attempts` / `run_attempts` | `patterns.toml` 中对应类别的 `max`（`run_attempts` → `run.*`；`stage_attempts` → `agent.*` / `gate.*`） |
| `autopilot.chatgpt_timeout_seconds` | 取 `min(原值, budget_*)` 写入对应阶段预算；原键废弃 |
| `autopilot.breaker.*`、`audit.*` | 不变 |
| `intake.max_in_hand` | `dispatch.in_hand`（0 仍表示自动，但自动的含义变为自适应） |
| `storage.disk_floor_gb` | `dispatch.disk.floor_gb` |
| `execution.run_timeout_seconds` / `attempts` / `stale_after_seconds` | 不迁移，仍属技能侧执行参数，留在设置页「执行」 |

## 7. API

所有写接口沿用 v2 的本机限制与错误体格式。旧的 `/api/autopilot/*` 与 `/api/intake/*` 保留为兼容外壳一个版本
（内部转调 Dispatcher），OpenAPI 标 `deprecated`。

### 7.1 调度

| 方法 路径 | 说明 |
|---|---|
| `GET /api/dispatch` | 调度快照：意图、有效状态、状态句、各池占用 / 上限、生效上限与弹性状态、挂起表、队列（瘦身卡片）、目标进度、吞吐摘要、守护命中数 |
| `POST /api/dispatch/state` | `{desired: "running" \| "paused" \| "draining"}`；启动时自动跑预检，P0 项不过返回 409 `PREFLIGHT_FAILED` |
| `POST /api/dispatch/interrupt` | `{task_ids?: [], kill_containers: true}` 立即中断；省略 `task_ids` = 全部 |
| `PATCH /api/dispatch/limits` | 容器数、弹性、会话、在手、间隔、录屏、预设；校验 → 写配置 → 同步技能并回读 → 返回新的生效值 |
| `PATCH /api/dispatch/policies` | 守护模式与阈值、网关守护、重试策略、磁盘、看门狗、定时窗口 |
| `GET /api/dispatch/explain?task_id=` | 一句话解释 + 门禁链逐项结果；省略 `task_id` 解释整条队列 |
| `GET /api/dispatch/upcoming` | 接下来启动的前 10 项与预计时间 |
| `GET /api/dispatch/throughput?hours=24` | 吞吐、阶段中位耗时、利用率 |
| `GET /api/dispatch/capacity?hours=24` | 容量时间线 |
| `GET /api/dispatch/log?after_seq=&family=&level=&task_id=` | 调度日志分页；实时走 SSE |

### 7.2 挂起与恢复

| 方法 路径 | 说明 |
|---|---|
| `GET /api/dispatch/holds` | 当前挂起与最近历史 |
| `POST /api/dispatch/holds/{id}/probe` | 立即探测 |
| `POST /api/dispatch/holds/{id}/release` | 手动解除（来源仍异常会再次挂起） |
| `POST /api/dispatch/holds/{id}/convert` | 转为人工暂停 |
| `POST /api/dispatch/probes/{source}` | 对任一来源手动探测（如「测试网关」） |
| `POST /api/dispatch/recover` | `{since_minutes: 120}` 清理残留并重新排队；返回各项数量 |

### 7.3 队列

| 方法 路径 | 说明 |
|---|---|
| `POST /api/dispatch/queue` | `{task_ids: []}` 加入调度（取代 autopilot `start` / `enroll`） |
| `POST /api/dispatch/queue/{task_id}/pin` · `/move` · `/retry` · `/quarantine` · `/release` · `/keep` | 置顶、排序、立即重试、隔离 / 解除、移出、标记保留 |
| `POST /api/dispatch/queue/clear-finished` | 清理已结束项 |

### 7.4 目标与守护

| 方法 路径 | 说明 |
|---|---|
| `GET / PUT /api/dispatch/plan` | 生产目标：模式、目标数、每日上限、项目与权重、优先、筛选与排除 |
| `POST /api/dispatch/plan/stop` | 终止目标，可选归还未开工的领取 |
| `GET /api/dispatch/guard` | 守护命中列表（含仅观察模式下的「将会做」） |
| `POST /api/dispatch/guard/{hit_id}/apply` · `/dismiss` | 手动执行一次处置 / 忽略该命中 |

### 7.5 健康

`GET /api/health` 扩展：`{ok, loops: [{name, status, last_beat_at, errors}], dispatch: {effective_state, holds}, starts}`。

### 7.6 SSE 事件

`dispatch.updated`（快照差量，≤ 2 帧 / 秒）、`dispatch.hold`（挂起增删）、`dispatch.limit`（生效上限变化）、
`dispatch.log`（调度日志行）、`guard.hit`。v2 的 `autopilot.updated` / `queue.updated` / `intake.updated` 继续发一个版本。

## 8. 界面与交互

### 8.1 信息架构变化

| 位置 | v2 | v3 |
|---|---|---|
| 导航第 2 项 | 生产线 | **调度**（生产线看板并入） |
| 导航 | — | 新增「调度日志」（放在设置之上；也可从调度页右上角进入） |
| 顶栏 | 运行 / 排队 / 阻塞三个计数链接 | 增加调度徽标：状态点 + 「3/4」+ 点击展开迷你面板（开关、容器数步进、当前挂起） |
| 设置页 | 执行、自动交付并发、在手上限等 | 这些字段移除，原位置留一行「调度参数已移到调度页 →」 |
| 看板收件箱 | 熔断告警等 | 熔断条目替换为挂起条目；只有「需要人」的挂起才进收件箱 |

### 8.2 调度页布局

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 调度   ● 运行中 · 容器 3/4 · 会话 5/6 · 在手 6/7 · 12 秒后启动下一题      [均衡 ▾]  [ ⏸ 暂停 ] [⋯] │ ← 页头
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ⚠ 领取已挂起：题源会话过期，自动重登失败（第 2 次，3 分钟后重试）   [立即探测] [去登录] [转为暂停]   │ ← 挂起条
├───────────────────────────────────────────────────────────┬─────────────────────────────────────┤
│ [全部 23] [需处理 2] [执行中 6] [等待 5] [已结束 10]   🔍    │ ▾ 调度上限                 已同步 ✓ │
│                                                           │   容器数      [-] 4 [+]   占用 3     │
│ 需处理                                                     │   弹性扩容    ( ●) 上限 [6] 生效 5   │
│  ▌cy-412  已隔离 · 门禁连续两次返回相同错误      [查看][重试] │   会话并发    自动 6                 │
│  ▌ld-090  待抽检                                 [去抽检]   │   在手上限    自动 7  （实测推导）   │
│                                                           │   启动间隔    [45] 秒                │
│ 执行中                                                     │   录屏并发    [1]                    │
│  起草 │ 运行 │ 交付 │ 导出      ← v2 生产线的阶段分列        │                    [还原] [应用]     │
│  ┌──┐ │┌──┐ │┌──┐ │                                        │ ▾ 生产目标        18 / 30 · 今日 21  │
│  │  │ ││  │ ││  │ │   每张卡：阶段已用时 · 尝试 n/m         │   模式 (●)目标数 ( )持续             │
│  └──┘ │└──┘ │└──┘ │   · 进展时间 · 守护标记                 │   项目与权重  A 60% · B 40%  [编辑]  │
│                                                           │   背压  待处理 4 / 12                │
│ 等待                                                       │ ▾ 自动启停                           │
│  1 cy-431  容器已满 4/4                                    │   网关  ● 正常 · 11 分钟前  [测试]   │
│  2 ld-102  排队第 2 位                                     │   Docker ● · 磁盘 ● 84 GB · 题源 ●   │
│  3 cy-377  3 分钟后第 2 次重试（网关限流）   [立即重试]      │   [清理残留…]                        │
│                                                           │ ▸ 兜底策略        仅观察 · 命中 1    │
│ 接下来启动：cy-431 约 12 秒 · ld-102 约 9 分钟 · …          │ ▸ 重试策略                           │
│                                                           │ ▸ 吞吐           24h 完成 31 · 中位 52′│
│ 已结束（折叠）                                             │ ▸ 容量时间线                         │
└───────────────────────────────────────────────────────────┴─────────────────────────────────────┘
```

要点：

- **主开关在页头右侧，始终可见**；文案随状态切换：暂停 / 启动 / 继续。`[⋯]` 里是「排空后停止」「立即中断…」「预检」。
- **状态句**由后端生成（`GET /api/dispatch` 的 `sentence`），前端不拼装，保证与门禁一致。
- **挂起条**：每条挂起一行，按范围由大到小排列；`auto=true` 的显示下次探测倒计时，`auto=false` 的显示「需要你处理」和主操作。
  没有挂起时整条不渲染。
- **调度上限面板默认展开**。步进器改动后「应用」按钮亮起；应用成功后右上角显示「已同步 ✓」（技能侧回读一致）
  或「技能侧为 6，已纠正」。派生字段灰底标「自动」。
- 调小容器数时，占用显示为「5 / 4 收敛中」，不弹确认框（不会中断任何东西）；调到低于当前弹性生效值时，生效值随之下调。
- **等待区每一项都有原因**；队首项的原因是队列级原因，其余要么是「排队第 n 位」，要么是自己的项级原因。
- 右栏其余面板默认折叠，标题行右侧显示一行摘要，不展开也能看到关键数字。
- 窄屏（< 1100 px）时右栏折到队列上方，变成横向可滚动的摘要条，点开为抽屉。

### 8.3 关键交互

| 场景 | 交互 |
|---|---|
| 首次点「启动」 | 自动预检；P0 不过则弹出 v2 的预检清单，不启动 |
| 暂停 | 立即生效，页头变「已暂停 · 3 个阶段仍在运行，跑完即停」 |
| 立即中断 | 弹窗列出将被中断的阶段，默认勾选「同时清理容器」；被中断的题标「已中断，可续跑」 |
| 改上限 | 应用后 1 拍内生效；Toast：「容器数 4 → 5，已同步到技能」 |
| 挂起出现 | 挂起条滑入；需要人的同时进收件箱并发系统通知；自动类只在调度页与顶栏徽标体现，不打扰 |
| 挂起解除 | 挂起条变绿停留 5 秒：「网关已恢复，挂起 18 分钟；已重新排队 3 题」 |
| 隔离的题 | 「需处理」分组；卡片展开显示失败类别、分类依据原文、已尝试次数、[从本阶段重试] [作废并归还] |
| 守护命中（仅观察） | 兜底策略面板计数 +1；展开看到「将会：清理容器 solo2611-xxx，依据：任务已终态 12 分钟」[执行] [忽略] |
| 解释器 | 任一等待项的原因文字可点，弹出门禁链逐项 ✓ / ✗ 清单 |
| 键盘 | `Space` 启停、`+` / `-` 容器数（需再按 `Enter` 应用）、`g d` 跳调度页、`g l` 跳调度日志 |

### 8.4 调度日志页

三栏头：级别筛选、事件族筛选（多选）、任务筛选。主体为虚拟滚动的事件行：时间 · 族 · 任务 · 中文说明，点击展开结构化详情。
顶部「跟随最新」开关；SSE 断线按 `after_seq` 续传。提供「按任务串联」视图：选中一道题后只显示它的事件，形成完整调度历程。

### 8.5 晨报

收件箱顶部一张可折叠卡片，内容由后端按调度日志聚合，纯事实不含建议：

```
昨晚 21:40 – 今晨 07:55
完成 17 题（待抽检 6）· 隔离 2 题 · 自动重试 9 次
挂起 3 次：网关 02:14–02:33（19 分钟，恢复后重新排队 3 题）· Docker 04:02–04:03 · 题源无候选 06:30 起
守护：清理僵尸容器 1 个
容器利用率 88%，中位单题 51 分钟
需要你处理：抽检 6 · 隔离题 2
```
