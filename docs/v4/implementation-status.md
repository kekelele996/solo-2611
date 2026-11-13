# v4 实现与验收状态

日期：2026-11-13
状态：**P0–P2 已实现并通过自动化检查；P1 已完成真实镜像探针、受管会话接受和监督器故障接回；P3 的长稳与平台试水尚未执行。**

## 1. 交付结论

- 技能已升级到 `0.6.0`，默认 `claude.runMode=supervised`；`print` 保留为回退。
- 工作台已升级到 `0.4.0`，运行执行器改为“发起 + 轮询”，SQLite 使用 schema v4。
- 一题只创建一次会话；中断时在原会话发送字面 `继续`，硬上限 2。
- 终止是终态；继续用尽、超时、无进展、等待处理超时、人工放弃均保留证据，不整题重跑。
- 备注、评分封顶、轨迹多轮校验、任务页会话面板、调度页容量/继续率、设置页新字段均已接入。

## 2. 自动化结果

| 检查 | 结果 |
|---|---|
| 技能测试 | `91 passed` |
| 工作台后端 | `ruff`、`mypy --strict`、`pytest 138 passed` |
| OpenAPI / TypeScript schema | `make check` contract 检查通过 |
| 前端 | typecheck、ESLint、25 个 Vitest、生产构建全部通过 |
| 静态门禁 | `make check` 的 grep 规则全部通过 |
| 发布脚本 | 两个临时目标同步后 `VERSION.json` 与 scripts 哈希一致 |

## 3. 真实运行证据

| 用例 | 结果 | 证据 |
|---|---|---|
| BL1 基线 | 120 个任务、259 次尝试；73 accepted、79 running、94 rejected；200.84 容器小时；0.3635 条/容器小时 | `docs/v4/evidence/baseline.json` |
| PR1 交互探针 | 官方镜像下中文逐字输入、实时轨迹 bind、Ctrl-C 后“继续”三项全部通过 | `docs/v4/evidence/probe-interactive.json` |
| 受管正常会话 | `run` 立即返回；监督器完成题面、轮次判定、收尾、轨迹收集和接受 | `docs/v4/evidence/managed-session-smoke.md` |
| SV1 监督器接回 | `kill -9` 后容器/tmux 保留，`resume` 起新 PID 接回；继续次数仍为 0，最终接受 | `docs/v4/evidence/supervisor-resume.md` |
| UI 渲染 | 任务页显示第 1 轮/继续 0/2/已接受；调度页显示等待继续与会话质量；设置页无旧“尝试次数”输入 | `docs/v4/evidence/ui-check.json` 与三张 PNG |

真实探针发现并修正了两个镜像行为差异：

1. 首次 interactive 启动有 Bypass Permissions 确认页，监督器现在先选择 `Yes, I accept`。
2. Claude Code `2.1.319` 在空闲 REPL 不接受两次 `C-d` 退出；收尾等待 5 秒后关闭 tmux，
   docker 客户端收到 EOF，容器正常退出且 bind 轨迹完整。

## 4. 已覆盖的实现

### 技能

- 原子状态写入、`state.lock`、监督器单例、心跳、事件日志。
- `run-status`、`resume`、`continue`、`abort`、`resume-all`、`doctor baseline`、
  `doctor --fix-stale`、`supervise`。
- 启动间隔锁、静默超时、Ctrl-C 脱离、`--hard-stop`。
- 实时轨迹增量读取、轮次结束判定、12 类中断分类、等待/继续/终止状态机。
- 多轮轨迹校验、时间线中断标记、备注合并、模型自身中断不得评 5 分。
- 双目录发布脚本。

### 工作台

- SQLite v4 可重入迁移和 `session_rounds`。
- `SoloState.sessionRun` 强类型模型，兼容旧任务。
- `SkillGateway` 新命令、`run-status` 封装和错误码分类。
- 轮询执行器、影子状态、SSE 会话事件、重启对账与 `resume`。
- `session_aborted`/`holding`/`manual_input` 状态映射。
- 会话 API、继续率/终止原因统计、队列接回。
- `SessionPanel`、容量分流、继续率卡和设置页 v4 项。
- 启动失败独立计数；运行失败类别不再重试。

## 5. 保守降级

- `claude.runMode=print`：一次机会、无继续；工作台检测到该模式时走兼容长连接执行器。
- `claude.continueMax=0`：中断即终止，仍不重跑。
- `dispatch.engine=v2`：调度写接口只读，技能侧受管会话继续运行。
- 已产生 `sessionRun` 与 `session_rounds` 在回退后保留只读。

## 6. 尚未执行

以下属于需要更长时间、多任务或平台外部状态的项目，当前没有伪造结果：

- T3.1 四题并发 E2E：未跑完整四场景。
- T3.2 八小时长稳、磁盘紧张、断网 60 秒、Docker 服务重启：未执行。
- T3.4 3–5 条带“继续”的真实数据提交与质检回执：未提交，避免未经单独确认触发平台外部写操作。
- LB1/LB2 八小时性能和三项吞吐目标对比：未执行。

## 7. 待项目方结论

| 编号 | 当前默认 | 状态 |
|---|---|---|
| Q1 | 本期照常收带“继续”的数据，备注使用固定句式 | 代码已按默认实现；平台质检尚未试水 |
| Q2 | 会话未成立前的启动失败不算机会，最多重新启动 2 次 | 代码已按默认实现 |
| Q3 | 达到总时长上限直接终止 | 代码已按默认实现 |

## 8. 发布状态

- 技能与工作台均在独立 `v4-session-recovery` 工作树完成，未覆盖生产 `~/.codex`、`~/.claude` 或 `~/.solo-auto`。
- 发布脚本已用两个临时目标验证；生产双目录同步待最终人工确认后执行。
