# v3 · 04 · 开发计划

## 1. 前提与约束

- 起点是当前工作区的 v2 实现。**开工前先把工作区里未提交的改动提交或搁置**（目前有 28 个已修改文件和 3 个未跟踪文件，不含本目录），
  v3 从一个干净的提交开始，否则回退开关无法验证。
- 不新增第三方依赖；`make check` 全绿是每个任务的完成条件之一。
- 每个任务一个提交，提交信息带任务号（`T11.3: …`）。
- 所有自动处置按「判定 → 仅观察输出 → 执行」三步实现，判定代码只有一份。
- 故障注入点与被测逻辑同一提交落地。
- 技能侧零改动是默认假设；需要技能配合的点都列在 `05-acceptance.md` §7 待决策项里，并各有不依赖技能的降级实现。

## 2. 里程碑

| 里程碑 | 主题 | 工期 | 交付后用户能感知到什么 | 依赖 |
|---|---|---|---|---|
| M11 | 调度内核与调度页骨架 | 6 天 | 调度页可用：开关、改并发即生效、队列带等待原因；重启不丢队列 | — |
| M12 | 挂起、探测、失败分类与重试 | 3.75 天 | 网关 / Docker / 磁盘 / 题源出问题会自己停、自己恢复；失败会退避重试；单题隔离 | M11 |
| M13 | 守护、认领、看门狗 | 3 天 | 重启不再误杀在跑的题；卡死的题有人管；后端卡死会自己重启 | M11 |
| M14 | 弹性容器与补位 2.0 | 2.75 天 | 容器上限按限流自动伸缩；多项目持续补位；背压 | M12 |
| M15 | 可观测与界面完善 | 2 天 | 吞吐、调度日志页、解释器、容量时间线、晨报 | M11–M13 |
| M16 | 故障注入、长稳演练、收尾 | 1.5 天 | 验收通过；回退开关验证；文档与状态记录 | 全部 |

合计 19 天（各里程碑工期 = §3 任务工期之和）。**M11 + M12 为最小可上线集合**（约 10 天）；M13 强烈建议一并上线（否则重启恢复仍是 v2 行为）。
M14、M15 互不依赖，可调换顺序。

## 3. 任务分解

工期单位为人天（d）。「文件」列中 ＋ 表示新增，Δ 表示修改。

### M11 调度内核与调度页骨架

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T11.1 | `DispatchSettings` 及子表、校验、TOML 读写、v2 → v3 配置迁移（一次性、可重入、保留原键） | Δ `settings.py`；＋ `dispatch/migrate.py`；Δ `config.example.toml` | 0.5 | DS1–DS3 |
| T11.2 | 时钟抽象（单调 / 挂钟 / 虚拟）、`LoopHealth` 骨架（先只注册与心跳，判定在 M13） | ＋ `dispatch/clock.py` `watchdog.py` | 0.25 | 单测 |
| T11.3 | 事实快照：Docker 单飞探测、超时、失败缓存、「未知」语义、`ps -a` 降级、磁盘、进程表 | ＋ `dispatch/facts.py` | 0.5 | CP2 CP3 A2 A3 |
| T11.4 | 落盘队列：表结构、迁移（从 autopilot 状态 JSON 与计划 JSON 生成）、排序函数、`upcoming` | ＋ `dispatch/queue.py`；Δ `store/repo.py` | 0.5 | DS4 QU1–QU4 |
| T11.5 | 资源池与容量账本：占用口径、预约、收敛中、外部容器 | ＋ `dispatch/pools.py` | 0.5 | CP1–CP6 |
| T11.6 | 门禁链 G1–G12（纯函数）+ 等待原因生命周期 + 启动间隔自适应 | ＋ `dispatch/gates.py` | 0.5 | GT1–GT12 |
| T11.7 | `Dispatcher`：运行意图、`gate` / `facts` / `advance` 三个循环、阶段申请与发放、上限写回技能并回读、漂移纠正 | ＋ `dispatch/kernel.py`；Δ `context.py` | 0.75 | CP7 CP8 DS5 |
| T11.8 | `RunScheduler` → `RunExecutor`：去掉自有 tick / 队列 / `refresh_limit`；槽位写真实 pid / pgid；修掉写死的 `attempt_total` | Δ `pipeline/scheduler.py` | 0.25 | 回归 v2 AP 用例 |
| T11.9 | `Autopilot` 接入：并发判断、入队、暂停判断改为委托 Dispatcher；`_advance` 推导逻辑不动 | Δ `pipeline/autopilot.py` | 0.5 | 回归 v2 AP 用例 |
| T11.10 | `/api/dispatch` 路由（state / limits / queue 操作 / 快照 / explain）+ 旧接口兼容外壳 + OpenAPI 再生成 | ＋ `api/dispatch.py`；Δ `api/autopilot.py` `api/intake.py` `api/types.py` | 0.5 | API1–API8 |
| T11.11 | 调度页骨架：页头开关与状态句、调度上限面板、队列分组与等待原因、阶段看板并入；导航替换；顶栏徽标；设置页移除已迁移字段 | ＋ `pages/DispatchPage.tsx` `components/dispatch/*`；Δ `AppShell.tsx` `SystemPage.tsx` `api/client.ts`；`ProductionPage.tsx` 改为重定向 | 1.0 | UI1–UI9 |
| T11.12 | 引擎回退开关 `dispatch.engine = "v2"`：按开关装配旧 / 新调度；两套共存的集成测试 | Δ `context.py` | 0.25 | RB1 RB2 |

M11 完成标准：v2 全部既有测试通过；调度页能完成「启动 → 改容器数 → 暂停 → 重启后端 → 队列还在」的完整操作。

### M12 挂起、探测、失败分类与重试

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T12.1 | `HoldRegistry`：登记 / 解除 / 范围查询 / 落盘 / 历史 / 最长自愈时间与升级 | ＋ `dispatch/holds.py` | 0.5 | HD1–HD6 |
| T12.2 | 探测框架 + 网关探测（复用 `Preflight.probe_api`，含 30 秒复测、即时触发去重、模型名校验） | ＋ `dispatch/probes/__init__.py` `llm.py` | 0.5 | B1 B2 B6 B8 |
| T12.3 | Docker / 磁盘探测与守护（含回滞、紧急瘦身触发） | ＋ `probes/docker.py` `disk.py`；Δ `pipeline/janitor.py` | 0.25 | A1 A4 A5 |
| T12.4 | 题源探测：自动重登、无候选退避、5xx 退避 | ＋ `probes/source.py`；Δ `sources/project_hub.py` | 0.25 | E1 E3 E4 |
| T12.5 | 会话 / 录屏 / 技能探测 | ＋ `probes/session.py` `recording.py` `skill.py` | 0.25 | D1 D2 F1 G8 |
| T12.6 | 失败分类器：`Evidence` 组装、六步判定、`patterns.toml` 加载校验热更新、默认特征表 | ＋ `dispatch/classify.py`；＋ 包内 `dispatch/default_patterns.toml` | 0.5 | CL1–CL8 |
| T12.7 | 恢复策略：类别表、退避、止损、隔离、工程冷却、总尝试上限、`unknown` 扎堆 | ＋ `dispatch/policy.py` | 0.5 | RT1–RT9 |
| T12.8 | `Breaker` 改造为挂起来源；`Autopilot._finish` 与 `RunExecutor` 的失败出口统一走 分类 → 策略 | Δ `pipeline/breaker.py` `autopilot.py` `scheduler.py` | 0.25 | 回归 v2 AP9 AP10 |
| T12.9 | 故障后清理残留（自动 + 手动入口） | ＋ `dispatch/recovery.py`（残留部分） | 0.25 | HD7 HD8 |
| T12.10 | 界面：挂起条、自动启停面板、隔离卡片、重试倒计时、重试策略面板；收件箱对接 | Δ `components/dispatch/*` `InboxPanel.tsx` | 0.5 | UI10–UI15 |

### M13 守护、认领、看门狗

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T13.1 | 守护框架：三档模式、命中记录、去重、手动执行 / 忽略、`keep` 标记 | ＋ `dispatch/guards/__init__.py` | 0.25 | GD1–GD3 |
| T13.2 | 进展信号采集（state / monitor / 轨迹 / workspace 跳过依赖目录 / Agent 日志，带上限的目录遍历）+ 无进展、运行超时、阶段预算守护 | ＋ `guards/progress.py` `timeout.py` | 0.5 | C5 C6 D8 D9 |
| T13.3 | 僵尸容器、孤儿容器、残留进程、领取泄漏、计数膨胀守护 | ＋ `guards/zombie.py` `orphan.py` `leak.py` `claims.py` `inflation.py` | 0.5 | C8 C9 E7 G3 |
| T13.4 | 重启认领：四象限处理、pid 命令行核对、日志跟随器、`collect` 收尾 | ＋ `dispatch/recovery.py`（认领部分）；Δ `gateway/skill.py`（按日志文件跟随已有进程） | 0.75 | RC1–RC6 |
| T13.5 | 副作用意图日志：表、写入点（领取 / 建仓 / 导出 / 提交 / 归还）、重启收敛 | ＋ `dispatch/intents.py`；Δ `refill`、`task_service.py`、`export_service.py`、提交相关 | 0.5 | RC7–RC10 |
| T13.6 | 看门狗判定与自退出、独立线程心跳、`/api/health` 扩展、launchd plist 调整 | Δ `dispatch/watchdog.py` `api/system.py` `scripts/` | 0.25 | A9 A10 |
| T13.7 | 启动记录与崩溃循环、睡眠唤醒检测与计时校正、`caffeinate` 管理 | Δ `dispatch/kernel.py` `clock.py` | 0.25 | A7 A11 |

### M14 弹性容器与补位 2.0

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T14.0 | **先验证信号**：在一道真实题上确认 429 重试事件的落盘位置与字段（Q-V3-1）；结论写进 `implementation-status.md` | — | 0.25 | — |
| T14.1 | 429 增量扫描器（按文件偏移续读尾部）、压力计算 | ＋ `dispatch/elastic.py` | 0.5 | EL1 EL2 |
| T14.2 | 升降规则、按小时记忆、状态落盘、与手动改保底的交互 | Δ `dispatch/elastic.py` | 0.5 | EL3–EL8 |
| T14.3 | 在手上限自适应与三档预设 | Δ `dispatch/pools.py` `kernel.py` | 0.25 | PR1–PR4 |
| T14.4 | 补位迁移：`IntakeService` → `dispatch/refill.py`；目标 / 持续模式、每日上限、跨日重置 | ＋ `dispatch/refill.py`；Δ `pipeline/intake.py`（留外壳） | 0.5 | RF1–RF4 |
| T14.5 | 多项目加权、优先项目、工程使用上限与冷却、背压 | Δ `dispatch/refill.py` | 0.5 | RF5–RF10 |
| T14.6 | 界面：弹性开关与生效值、生产目标面板、项目权重编辑、背压显示 | Δ `components/dispatch/*` | 0.25 | UI16–UI19 |

### M15 可观测与界面完善

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T15.1 | `outcomes` 记录与吞吐统计、容量时间线采样 | ＋ `dispatch/throughput.py` | 0.5 | OB1–OB3 |
| T15.2 | 调度日志：事件族规范化、`dispatch.jsonl` 镜像与轮转、分页与 SSE 续传 | Δ `store/repo.py` `api/dispatch.py` `events/bus.py` | 0.25 | OB4 OB5 |
| T15.3 | 调度日志页、按任务串联视图 | ＋ `pages/DispatchLogPage.tsx` | 0.5 | UI20 UI21 |
| T15.4 | 解释器弹层、接下来启动、吞吐卡片、容量时间线（SVG）、兜底策略面板 | Δ `components/dispatch/*` | 0.5 | UI22–UI26 |
| T15.5 | 晨报、诊断包追加、命令面板命令、定时窗口（P2，可砍） | Δ `api/system.py` `CommandPalette.tsx` `InboxPanel.tsx` | 0.25 | OB6 OB7 |

### M16 故障注入、长稳演练、收尾

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T16.1 | 故障注入矩阵补齐：`05` §3 每一条都有自动化用例或演练脚本 | Δ `tests/`；＋ `tests/chaos/` | 0.5 | §3 全部 |
| T16.2 | 长稳演练脚本（假技能 + 假 Docker + 虚拟时钟加速 / 真实时钟 12 小时两种） | ＋ `scripts/soak.py` | 0.5 | §4 |
| T16.3 | 回退验证、迁移往返验证、文档收尾（`docs/v3/implementation-status.md`、`README.md`、`PROMPT.md` 更新） | — | 0.5 | RB1–RB4、DoD |

## 4. 改动影响面

| 现有文件 | 改动性质 | 风险点 | 缓解 |
|---|---|---|---|
| `pipeline/scheduler.py` | 拆除调度职责，保留执行 | 手动「运行」入口（任务详情页）仍要能用 | 手动运行也走 Dispatcher 入队（置顶），保留 `POST /tasks/{id}/run` 语义 |
| `pipeline/autopilot.py` | 并发 / 失败 / 暂停出口改委托 | 1294 行的大文件，`_advance` 与 ChatGPT 分支交织 | 只改四个出口（`_launch` 的并发判断、`_launch_chatgpt` 的并发判断、`_finish`、`tick` 的暂停判断），不重排逻辑；每改一个出口跑一遍 `test_autopilot.py` |
| `pipeline/intake.py` | 迁移到 `refill.py` | 计划 JSON 格式变化 | 读旧格式写新格式；外壳接口保留一个版本 |
| `pipeline/breaker.py` | `trip()` 改为登记挂起 | 熔断状态文件 | 启动时把已熔断状态转成一条 `breaker` 挂起 |
| `context.py` | 装配与循环注册 | 启动顺序 | 顺序固定为：实例锁 → 配置迁移 → 表迁移 → 对账 → 认领 → 意图收敛 → 启动循环 → 宽限期后开门 |
| `settings.py` / `api/system.py` | 字段迁移 | 设置页表单、OpenAPI、前端类型 | 旧字段只读返回并标 `moved_to`；`make check` 的契约检查覆盖 |
| `SystemPage.tsx` | 移除字段 | 现有 `systemPage.test.tsx` | 同步改测试 |
| `ProductionPage.tsx` | 被 `DispatchPage` 取代 | 路由与书签 | `/production` 重定向到 `/dispatch` |

不动的部分：证据（轨迹 / diff / 录屏）、Feedback 复核、抽检、批次与导出逻辑、回收站、使用统计、技能网关的调用协议。

## 5. 迁移与回退

### 5.1 升级路径（v2 → v3）

1. 启动时检测到没有 `[dispatch]` 表 → 执行配置迁移（映射见设计 §6.3），原键保留。
2. `schema_version` 升级：建新表；从 autopilot 状态与计划 JSON 生成 `dispatch_items`（活跃题进 `queued` / `active`，失败题进 `quarantined`，
   已完成题不迁移）。
3. 已熔断 → 转为 `breaker` 挂起；已暂停 → `desired_state = paused`；正在运行 → `running`。
4. 迁移全程写 `dispatch.migrated` 事件，含每一步的条数。迁移失败则保持 `engine = "v2"` 启动并告警，不半途而废。

### 5.2 回退路径（v3 → v2）

- `dispatch.engine = "v2"` 后重启：装配 v2 的 `RunScheduler` / `Autopilot` / `IntakeService`，读原键（迁移时未删除）。
- v3 期间新入队的题在 v2 的 autopilot 状态 JSON 里没有记录——回退时由一个反向导出步骤把 `dispatch_items` 中的活跃项写回 v2 格式。
- v3 新表保留不删；再次切回 v3 时以表中数据为准并对账。
- 回退开关保留一个版本周期，下个版本删除 v2 调度代码。

### 5.3 发布顺序建议

| 阶段 | 配置 | 观察什么 |
|---|---|---|
| 第 1–2 天 | `engine=v3`，`guard.mode=observe`，`elastic.enabled=false`，预设「稳妥」 | 等待原因是否准确；守护命中是否有误报 |
| 第 3–5 天 | 预设「均衡」；确认无误报后 `guard.mode=enforce` | 容器利用率；自愈事件 |
| 之后 | 按需开启弹性、持续补位 | 弹性升降是否合理 |

## 6. 测试策略

| 层 | 范围 | 手段 |
|---|---|---|
| 单元 | 门禁链、容量账本、排序、分类器、策略表、弹性规则、预设推导、挂起表 | 纯函数 + 虚拟时钟；门禁链做表驱动的全组合测试 |
| 组件 | Dispatcher 循环、探测器、守护、认领、意图收敛 | 假技能（可编程退出码 / 耗时 / 输出）、假 Docker（可编程容器列表与失败）、`httpx.MockTransport` 题源与网关 |
| 故障注入 | `03` 每一条 | `dispatch/faults.py` 提供的注入点 + 环境变量 `SOLO_AUTO_FAULTS`；注入点清单见 `05` §3.1 |
| 性质测试 | 「永不超发」「副作用至多一次」「人工暂停不被解除」 | 随机事件序列（启动 / 完成 / 失败 / 探测翻转 / 改上限 / 杀进程重启）跑 1 万步，每步断言不变量 |
| 前端 | 调度页状态渲染、上限面板、挂起条、解释器 | Vitest + 固定快照夹具；每种有效状态一份快照 |
| 端到端 | 真实技能 + 真实 Docker 小规模 | 2 题、容器数 2，人为拔网 / 重启 Docker / 杀后端各一次 |
| 长稳 | 12 小时 | `scripts/soak.py`，见 `05` §4 |

覆盖率要求：`dispatch/` 包行覆盖 ≥ 90%，`gates.py` / `pools.py` / `policy.py` / `classify.py` 分支覆盖 ≥ 95%。

必须有的三条不变量断言（任何测试中违反即失败）：

```
I1  任一时刻：平台启动且未结束的运行数 + 外部容器数 ≤ 生效上限（收敛中除外，且收敛中不得有新启动）
I2  任一幂等键：状态为 done 的同类意图至多一条；对外副作用调用次数 ≤ 1
I3  desired_state = paused 期间：没有任何新阶段被启动；任何自动路径都不会把它改成 running
```

## 7. 风险登记

| # | 风险 | 可能性 | 影响 | 应对 |
|---|---|---|---|---|
| R1 | 429 信号在 2611 的轨迹里拿不到或格式不同 | 中 | 弹性做不了 | T14.0 先验证；拿不到则弹性保持关闭，B4 降级为「只告警 + 建议」 |
| R2 | 守护误杀正常运行的题 | 中 | 白跑、浪费额度 | 默认仅观察；双重确认；进展信号取并集；睡眠时长扣除；上线前 2 天只看命中 |
| R3 | 认领时 PID 复用导致误认 | 低 | 错误占用或误杀 | 核对命令行含任务目录 + 进程启动时间晚于槽位写入时间则不认 |
| R4 | `autopilot.py` 改造引入回归 | 中 | v2 已验收能力退化 | 只改四个出口；v2 全量用例作回归门；回退开关 |
| R5 | 上下文归因把题目自身的问题也算成基础设施问题，导致无限免费重试 | 中 | 一道坏题反复占名额 | 单题总尝试上限 8；归因窗口只有 120 秒；归因失败也进冷却统计 |
| R6 | 自动重登把错误凭据反复提交导致账号被锁 | 低 | 题源不可用 | 自动重登每次挂起只做 1 次，之后指数退避；3 次失败即升级不再尝试 |
| R7 | 独立线程自退出误判（长 GC / 机器极卡） | 低 | 无谓重启 | 阈值 120 秒；退出前落盘；崩溃循环保护兜底 |
| R8 | 技能 `collect` 对「平台启动的非交互运行」不适用 | 中 | 认领的「死 × 活」「死 × 无」分支无法收尾 | Q-V3-2 确认；不适用时降级为：等容器结束 → 标该次尝试中断 → 不计次数重跑 |
| R9 | 多项目补位使单项目计划的既有统计口径变化 | 低 | 使用统计对不上 | 台账本来就按项目 / 工程记录，不受影响；计划进度按项目分列 |
| R10 | `caffeinate` 让机器整夜不睡，用户不知情 | 低 | 耗电 / 发热 | 页头状态句旁显示「已阻止睡眠」图标；`keep_awake` 可关 |
| R11 | 调度页一次塞进太多面板，信息过载 | 中 | 反而难用 | 只有「调度上限」默认展开；其余折叠且标题行带摘要；窄屏走抽屉 |
| R12 | 工作区现有未提交改动与 v3 改动相互覆盖 | 高（当前就有） | 丢代码 | §1 的前提：先提交 |
