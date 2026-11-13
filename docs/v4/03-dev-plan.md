# v4 · 03 · 开发计划

> 起点：技能 `solo-2611` 0.5.0；工作台 `solo-auto` v0.3.0（`dispatch.engine = "v3"`，`main` 分支）。
> 规则见 `01-requirements.md` §3，实现细节见 `02-design.md`。
> 工期单位为人天（d）。「验收」列对应 `04-acceptance.md` 的用例编号。

## 1. 前提与约束

- 不新增第三方依赖。tmux 已在本机（3.6a），作为系统依赖写入预检与 README。
- 技能两份副本（`~/.codex/skills/solo-2611`、`~/.claude/skills/solo-2611`）**必须同步发布**，
  发布脚本在 P1 末尾补上（T1.9）。
- 工作台 `make check` 全绿是每个任务的完成条件之一；每个任务一个提交，提交信息带任务号（`T1.3: …`）。
- 技能侧改动要先在隔离目录（`SOLO2611_TASKS_DIR=/tmp/solo2611-accept`）跑通真实容器，再动生产。
- 开发期间不修改 `~/solo-auto` 下正在被真实任务使用的状态；工作台改动在独立 worktree 进行（沿用 v3 做法）。
- 规则改动（`01` §3）先落到技能，工作台只消费技能状态。两者解耦，可分批上线。

## 2. 里程碑

| 阶段 | 主题 | 工期 | 交付后能感知到什么 | 依赖 |
|---|---|---|---|---|
| P0 | 只读止血（不改运行方式） | 2 天 | 悬空不再作废：说"继续"就接回正在跑的容器；启动错开；超时与无进展判定不再瞎杀；基线报表 | — |
| P1 | 受管交互与"继续"闭环 | 6 天 | 会话中断自动发"继续"（≤2）；终止取代重跑；备注与评分同步 | P0 |
| P2 | 工作台改造 | 4.5 天 | 运行不再依赖长连接；重启零影响；会话面板；统计与容量 | P1 |
| P3 | 联合演练与试水 | 2 天 | 全链路验收；3–5 条带"继续"数据试提交；回退验证 | P1 P2 |

合计 **14.5 天**。P0 可独立上线，且是后续所有步骤的兜底（新功能出问题时 P0 的收益仍在）。
**P0 建议立即开工**：它不改变任何数据口径，纯收益。

## 3. 任务分解

### P0 · 只读止血（技能侧，不改变数据口径）

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T0.1 | 基线脚本：扫描 tasksDir 下全部 `state.json`，输出尝试状态分布、失败原因分类、容器时长、每容器小时产出，写 `monitor/baseline.json` | ＋ `scripts/doctor.py`（`baseline` 子命令） | 0.5 | BL1 |
| T0.2 | 交互模式探针：在隔离目录起一个 `interactive` 容器，验证 ① 中文 `send-keys -l` 能正确进入输入框；② `projects` 目录能否 bind 挂载；③ API 错误后 REPL 是否接受输入（模拟 429 或直接输入一条会触发错误的内容不现实时，改为：发一个提示词后 `Ctrl-C`，再发"继续"，看是否接受）。结论写 `references/lessons-learned.md` | ＋ `scripts/probe_interactive.py` | 0.5 | PR1 |
| T0.3 | 启动间隔锁 `launch.lock`：`_docker_base` 前取锁，距上次启动不足 `claude.launchSpacingSeconds`（默认 20）则等待；新增配置项 | Δ `scripts/solo2611.py` `common.py` | 0.25 | SP1 SP2 |
| T0.4 | 超时判定改为"静默计时"：不再固定 7200 秒一刀切，改为"容器连续 `silentSeconds`（默认 1200）秒无 stdout 输出才终止"，总长仍受 `timeoutSeconds` 约束 | Δ `scripts/solo2611.py` `cmd_run` | 0.25 | TO1 TO2 |
| T0.5 | Ctrl-C 改为"脱离不终止"：捕获后不 `docker stop`、不 `_reset_workspace`，写 `state.status` 不变，打印接回指令；新增 `--hard-stop` 保留旧的立即终止行为 | Δ `scripts/solo2611.py` | 0.15 | CC1 CC2 |
| T0.6 | `resume`（print 版）：找最后一条 `running` 尝试，按容器状态处理——存活则 `docker wait` 接回原超时；已退出未删则走 `_collect_container`；不存在则标 `lost` 并提示 `reset --yes` | ＋ `scripts/resume.py`；Δ `solo2611.py` 参数表 | 0.5 | RS1–RS4 |

P0 完成标准：在真实 Docker 上演练"起 `run` → 杀掉 Python 进程 → `resume` → 拿到 accepted 结果"，
并且 `baseline.json` 能产出 §1 的四个指标。**P0 不需要改任何标注规范，不改 `SKILL.md` 的固定口径**，
只在「工作流」一节补 `resume` 用法。

### P1 · 受管交互与"继续"闭环（技能侧）

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T1.1 | `session.py` 骨架：tmux 会话管理（起/查/送字/capture-pane/杀）、`env.sh` 注入 Key、`launching` 状态与输入框就绪探测 | ＋ `scripts/session.py` | 0.75 | SG1–SG3 SP3 |
| T1.2 | 实时轨迹：`live/` 目录 bind、sessionId 发现、增量读尾部、轨迹不完整时的容错 | Δ `session.py` `common.py` | 0.5 | TR1 TR2 |
| T1.3 | 轮次结束判定 `round_ended`：复用 `trace._content` 等，手工实现"最后一条 assistant + 无悬空工具调用 + 文件静默 + 屏幕空闲" | ＋ `session.py` 内的 `round_ended` | 0.5 | RE1–RE5 |
| T1.4 | 中断分类 `classify_round`：`02` §3.2 十二行表逐条实现，复用 `trace.ASKING_USER/PLAN_PROMISE/CUT_OFF/INCOMPLETE_TAIL` | ＋ `session.py`；Δ `trace.py` 提取常量 | 0.5 | CLS1–CLS12 |
| T1.5 | 状态机与"继续"预算：`continues.used/max`、等待、发送、`holding`、`manual_input`、`abort` | ＋ `session.py` | 0.75 | SM1–SM8 CN1–CN4 |
| T1.6 | 监督器进程与单例锁、心跳、`supervise` 子命令 | ＋ `session.py`；Δ `solo2611.py` | 0.5 | SV1 SV2 |
| T1.7 | `run` 重写为受管模式（默认）+ `--mode print` 回退；`_collect_container` 改用 `traces/` 拷贝并接新校验 | Δ `solo2611.py` | 0.5 | RN1–RN3 |
| T1.8 | 轨迹校验改写：`analyze` 的多轮放行；`completion_review` 只看最后一轮 | Δ `scripts/trace.py` | 0.5 | VA1–VA5 |
| T1.9 | `run-status` / `resume` / `continue` / `abort` / `resume-all` / `doctor --fix-stale` 命令行与 JSON 输出 | Δ `solo2611.py`；＋ `resume.py` | 0.75 | CLI1–CLI7 |
| T1.10 | 事件日志 `supervisor.log` 全事件类型、时间线中断标记、`终止说明.md` | Δ `session.py` `trace.py` | 0.25 | LG1 LG2 |
| T1.11 | 备注与提交：`_submission_remark` 合并 Key 风险与继续说明，`build_row` / `submit` / `repair` 三处统一 | Δ `solo2611.py` `submission.py` | 0.25 | RM1–RM3 |
| T1.12 | 评分与文案指南：`feedback --check` 封顶规则、`feedback-guide.md`/`score-calibration.md` 条款 | Δ `feedback.py` `references/*.md` | 0.25 | SC1 SC2 |
| T1.13 | 文档与版本：`SKILL.md`（固定口径、工作流、"继续"章节、CLI 速查）、`container-spec.md`、`annotation-spec.md`、`VERSION.json` → 0.6.0 | Δ 文档 | 0.5 | DOC1–DOC3 |
| T1.14 | 发布脚本：把技能目录同步到 `~/.codex` 与 `~/.claude` 两处并校验 `VERSION.json` 一致 | ＋ `scripts/release.sh`（技能仓库内） | 0.15 | REL1 |
| T1.15 | 单元测试：`tests/test_session.py`，覆盖状态机、分类表、预算、等待（用假的轨迹与假 tmux） | ＋ `tests/test_session.py` | 0.5 | 全部自测类 |

P1 完成标准：在隔离目录跑两道真实题——一道正常完成，一道人为制造一次 429（可用错误 Key 或压测限流），
自动发出"继续"并最终被接受；再跑一道人为中断两次后用尽机会，得到 `aborted` 与 `终止说明.md`。

### P2 · 工作台改造

| 任务 | 内容 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| T2.1 | SQLite 第 4 版迁移：`attempts` 新列、`session_rounds`、`dispatch_items` 新列、`dispatch_capacity.waiting`；可重入 | Δ `store/repo.py`；＋ `migrations/v4.sql` | 0.5 | MG1–MG3 |
| T2.2 | `SoloState` 模型扩展 `sessionRun`（`store/state.py`），`extra="allow"` 已有，只补强类型字段供界面用 | Δ `store/state.py` | 0.25 | MG4 |
| T2.3 | `SkillGateway` 新命令白名单与 `run-status` 轮询封装、错误码分类（1/2/其他） | Δ `gateway/skill.py` | 0.5 | GW1 GW2 |
| T2.4 | `RunExecutor` 改为"发起 + 轮询"；影子状态写入 `run/sessions/<task_id>.json`；SSE 事件 | Δ `pipeline/scheduler.py` | 0.75 | EX1–EX5 |
| T2.5 | 重启认领：`reconcile` 对 `running` 任务调 `run-status` → 必要时 `resume`；失败标 `session_lost` | Δ `pipeline/reconcile.py` `pipeline/autopilot.py` | 0.5 | RC1–RC3 |
| T2.6 | 失败策略：`default_patterns.toml` 新类别与 `run.*` 计费归零；`retry` 上限改造；`autopilot.run_attempts` 固定 1 | Δ `dispatch/default_patterns.toml` `dispatch/policy.py` `pipeline/autopilot.py` `settings.py` | 0.5 | PT1–PT3 |
| T2.7 | 会话内限流/5xx 不再上报任务失败：`scheduler` 与 `autopilot` 的失败出口按 `lastKind` 分流 | Δ `pipeline/scheduler.py` `pipeline/autopilot.py` `dispatch/classify.py` | 0.5 | FL1 FL2 |
| T2.8 | 容量与弹性：`waiting_continue`/`holding` 计入占用；弹性读取等待时长；容量页数据 | Δ `dispatch/pools.py` `dispatch/elastic.py` `dispatch/kernel.py` | 0.5 | CP1–CP3 |
| T2.9 | 守护：无进展与超时以技能 `deadline` 为准；`keep_awake` 不误判等待态 | Δ `dispatch/guards/progress.py` `guards/timeout.py` | 0.25 | GD1 GD2 |
| T2.10 | 状态映射：`session_aborted`、`holding`、`manual_input` → `aborted`/`blocked`；`derive_job_status` 同步 | Δ `pipeline/steps.py` `api/types.py` | 0.25 | SM-W1 SM-W2 |
| T2.11 | 接口：`resume` / `continue` / `abort` / `session` / `session-stats` / 队列接回；OpenAPI 再生成 | ＋ `api/session.py`；Δ `api/tasks.py` `api/dispatch.py` `api/types.py` | 0.5 | API1–API6 |
| T2.12 | 界面：`SessionPanel`、任务页轮次时间线、调度页容量与继续率、设置页新项与移除项 | ＋ `components/task/SessionPanel.tsx`；Δ `TaskDetailPage.tsx` `DispatchPage.tsx` `SystemPage.tsx` `api/client.ts` | 0.75 | UI1–UI8 |
| T2.13 | 终止后的归还与补位：`aborted` 自动归还领取、进冷却、触发补位 | Δ `pipeline/autopilot.py` `dispatch/kernel.py` | 0.25 | RB1 RB2 |
| T2.14 | 文档：`docs/13` 增量、`docs/v3/03` 类别表、`docs/04` 状态映射、`docs/v4/implementation-status.md` 模板 | Δ 文档 | 0.25 | DOC4 |

P2 完成标准：v3 既有测试全绿；工作台重启不影响在跑会话；界面上能看到轮次与继续次数；
`make check` 全绿（含新增 3 条 grep 规则：不得在 `dispatch/` 之外调 `docker ps`；
不得直接写 `state.json`；不得出现把 `continues.max` 写成大于 2 的常量）。

### P3 · 联合演练与试水

| 任务 | 内容 | 工期 | 验收 |
|---|---|---|---|
| T3.1 | 端到端演练：4 道题并发，其中 1 道正常、1 道限流继续 1 次、1 道继续用尽终止、1 道人为 `abort`；核对库、界面、事件、交付物 | 0.75 | E2E1–E2E6 |
| T3.2 | 故障注入：杀监督器、杀工作台后端、断网 60 秒、docker 重启、磁盘紧张，逐项核对不丢会话、不误终止 | 0.5 | FI1–FI5 |
| T3.3 | 回退验证：切 `claude.runMode=print` 与 `dispatch.engine=v2`，确认可回到旧行为，正在跑的会话不受影响 | 0.25 | RB3 RB4 |
| T3.4 | 试水：3–5 条带"继续"的真实数据走完导出与提交，记录质检结果，回填 Q1 结论 | 0.5 | PW1 |
| T3.5 | 长稳：连续 8 小时调度，统计继续率、终止率、每容器小时产出，与 T0.1 基线对比 | 0.5 | LB1 LB2 |

## 4. 上线与回退

### 4.1 上线顺序

1. **P0 单独发布**（技能 0.5.1）：只加 `resume`、静默超时、启动间隔、Ctrl-C 行为。
   发布 → 用旧方式跑一天的题，确认无回归。
2. **P1 发布**（技能 0.6.0）：`claude.runMode` 默认 `supervised`。先只给 1 道题跑，
   人工盯完整程；通过后放开到 2 道并发。
3. **P2 发布**（工作台 v4）：先 `dispatch.engine = "v3"` 下确认新接口可用，
   再用 `SOLO_AUTO_DISPATCH__ENGINE=v3` 全量。技能与工作台的联调点只有 `run-status` 与 `resume`，
   两者独立可用。
4. **P3 完成后**再放开到容器上限 3–4 并发。

### 4.2 回退

| 需要回退的现象 | 回退动作 | 影响 |
|---|---|---|
| 交互 TUI 输入不稳定 | `config set claude.runMode=print` | 新会话回到无头、一次机会；已开着的会话继续按新规则跑完 |
| 平台打回带"继续"的数据 | `config set claude.continueMax=0` | 中断即终止，不再发继续；不重跑 |
| 工作台新执行器异常 | `dispatch.engine = "v3"` 或停用 v4 路由 | 任务仍在技能侧跑完，只是工作台不跟踪 |
| 监督器有 bug | 技能降级到 0.5.1（P0 版本） | 保住"接回"，失去"继续" |

**回退不动的部分**：已经产生的 `sessionRun` 字段与 `session_rounds` 表保留只读，不删不改。

## 5. 风险

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 交互 TUI 输入中文或换行异常 | 中 | 高（阻塞 P1） | T0.2 探针先行；失败则改"题面写文件 + 首条消息只写一句指令"（需项目方同意） |
| 平台对多轮轨迹的质检口径不明 | 高 | 高 | P3 试水 3–5 条；Q1 未确认前不放开全量 |
| 挂载 `projects` 被镜像入口拒绝 | 低 | 中 | `docker exec cat` 轮询兜底 |
| 监督器与工作台双重判定导致状态打架 | 中 | 中 | 明文规定：终态只由技能产生，工作台只读 |
| 等待期间容器占满名额导致产能下降 | 中 | 中 | 总时长与无进展双上限；容量页显式显示等待项 |
| 429 高发时容器堆积 | 中 | 中 | 工作台启动间隔 + 弹性下调；429 等待时间指数退避 |
| tmux 中 Key 泄露 | 低 | 高 | `env.sh` 0600、用后删除、grep 检查 `ps` 输出 |

## 6. 交付物清单

- 技能 `solo-2611` 0.6.0：`scripts/session.py`、`resume.py`、`doctor.py`、`probe_interactive.py`、
  `tests/test_session.py`、`release.sh`，以及 `SKILL.md` 与 4 份 references 的增量。
- 工作台 `solo-auto` v4：`migrations/v4.sql`、`api/session.py`、`SessionPanel.tsx`，
  以及 12 个既有文件的修改。
- 文档：本目录四份 + `implementation-status.md`。
- 演练记录：`docs/v4/evidence/` 下的演练日志、`baseline.json`、试水质检截图。
