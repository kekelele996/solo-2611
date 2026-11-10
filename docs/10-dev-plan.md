# 10 · 开发计划

## 1. 前提

- **本文档由需求侧编写，交由其他模型执行开发。** 因此每一节都写成「可直接开工」的粒度：
  明确的输入、输出、依赖顺序、验收方式。
- 假设开发者为 1 个 AI 编码 Agent（或 1 人），每天有效产出 4–6 小时。
- 总工期估算 **12–15 个工作日**（不含真实试跑的 2 天）。

## 2. 里程碑

```
M0 骨架可跑        D1–D2     后端起服务、前端起页面、/api/health 通
M1 事实层          D3–D5     技能网关、状态解析、11 步判定、SQLite 对账
M2 题源与建题      D6–D7     工程台适配、领取、ZIP、init
M3 运行与证据      D8–D10    队列、容器槽位、轨迹、diff、录屏
M4 反馈与导出      D11–D12   Feedback 门禁、复核、xlsx 导出
M5 打磨与验收      D13–D15   UI 细节、动效、快捷键、端到端演练
试跑               D16–D17   连续两天真实作业
```

**M1 完成前不要碰 UI。** 事实层错了，界面做得再好也是错的数据。

## 3. 任务分解

每项任务标注：`ID · 目标 · 产出 · 依赖 · 验收`

### M0 骨架（D1–D2）

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T0.1 | 后端骨架 | `backend/pyproject.toml`、`solo_auto/app.py`（FastAPI + StaticFiles + lifespan）、`/api/health`、`/api/version` | — | `uvicorn solo_auto.app:app` 启动，两接口 200 |
| T0.2 | 配置加载 | `settings.py`（pydantic-settings，读 `config.toml`）、`secrets.py`（Keychain 封装 + 0600 文件回退） | T0.1 | 改 `config.toml` 后重启生效；`python -c "from solo_auto.secrets import get"` 能取到值 |
| T0.3 | 日志与事件骨架 | `logs/app.log` 滚动、`events` 表建表、`store/repo.py` 的基础 CRUD | T0.1 | 手动插一条 event，`sqlite3` 可查 |
| T0.4 | 前端骨架 | Vite + React 19 + TS、Router、Query client、`AppShell`（顶栏 + 左导航）、空白 5 页 | — | `npm run dev` 打开 5173，5 个路由可跳 |
| T0.5 | 设计令牌 | `styles/tokens.css`（照抄 `05` §2.2 与 `07` §7）、`styles/base.css`（reset + 字体 + focus） | T0.4 | 页面背景为 `--paper`，Tab 键有可见 outline |
| T0.6 | 单实例锁 + 健康自检 | `run/solo-auto.pid`、启动时检查 Docker/磁盘/技能路径 | T0.1 | 启动第二个实例被拒；Docker 停时启动给出警告横幅 |

### M1 事实层（D3–D5）—— 最关键

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T1.1 | 技能网关 | `gateway/skill.py`：路径探测（`~/.codex/skills/solo-2611` 与 `~/.claude/skills/solo-2611`）、`version` 探测、命令白名单、超时、stdout/stderr 捕获、`SkillResult{code, out, err, duration}` | T0.2 | 调 `version` 返回技能版本；调一个不存在命令被白名单拒绝 |
| T1.2 | 状态解析 | `store/state.py`：读 `state.json`，pydantic 模型，未知 schema 走 `RawState` 兜底 | T1.1 | 用真实 `~/claude-runs/solo-2611/<某任务>/monitor/state.json` 解析成功并打印关键字段 |
| T1.3 | 11 步判定 | `pipeline/steps.py`：11 个判定函数 + `evaluate(task_root) -> list[Step]` | T1.2 | 跑 `09` §3.2-A 的 A1–A13 全部用例 |
| T1.4 | SQLite 索引 | `store/repo.py`：6 张表 DDL、CRUD、`UNIQUE(session_id)` | T0.3 | 插两条相同 session_id，第二条报 IntegrityError |
| T1.5 | 对账 | `reconcile()` + 60s 定时 + `POST /api/system/reconcile` | T1.3, T1.4 | 手改 state.json 后 60s 内 steps 表更新，events 有 `reconcile.corrected` |
| T1.6 | 文件监视 | `store/state_watch.py`（watchdog），变化 → 标记 dirty → 合并后单次 reconcile | T1.5 | 连续改 5 次只触发 1 次 reconcile（100ms 去抖） |
| T1.7 | 只读浏览 UI | 任务列表页：扫描 `TASKS_DIR` 列出全部任务 + 状态 + 11 步 | T0.5, T1.3 | 能看到现有的真实任务，状态与命令行 `status` 一致 |

> **M1 的判定标准**：不写任何业务逻辑，仅靠读磁盘，就能把现有 `~/claude-runs/solo-2611/` 下的
> 真实任务全部正确呈现。做到这一点，后面所有功能才有了可靠的地基。

### M2 题源与建题（D6–D7）

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T2.1 | 适配器接口 | `sources/base.py`（Protocol）：`list_projects` / `list_engineering` / `claim` / `release` / `download_zip` / `detail` | T0.2 | 抽象定义齐全，`local_dir.py` 可实现 |
| T2.2 | 工程台客户端 | `sources/project_hub.py`：Bearer 读、Cookie+CSRF 写、分页、ZIP 流式下载、错误映射（401/403/404/409） | T2.1 | 对 `localhost:8088` 能列出项目与工程；MockTransport 跑通 C1–C7 |
| T2.3 | 暂存区 | `staging/<eid>/`：下载、sha256 校验、解包（注意 zip-slip 防护）、写 `meta.json` | T2.2 | C3 用例通过；解包后目录里无 `../` 逃逸 |
| T2.4 | 领取/归还 API | `POST /api/claims`、`DELETE /api/claims/{id}`（含清理容器的前置检查） | T2.2, T2.3 | FR-SRC-05/07 验收通过 |
| T2.5 | 建任务 | `POST /api/tasks` → 调 `init` → 写 tasks 行 → 事件 | T1.4, T2.3 | 建完后目录结构完整，`state.json` 存在 |
| T2.6 | 题源页面 | `/source`：项目下拉（记住上次）、工程表（服务端筛选）、领取 Modal、已领取区 | T0.5, T2.2 | FR-SRC-02/03/04/05 界面验收 |
| T2.7 | 离线降级 | 题源不可达 → `offline` 模式，禁新建 | T2.2 | FR-SRC-09 |

### M3 运行与证据（D8–D10）

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T3.1 | 容器槽位 | `run/containers.lock` + `slots/`、flock、僵尸回收 | T0.6 | B1/B3 用例 |
| T3.2 | 调度器 | `pipeline/scheduler.py`：2s tick、队列、优先级、开跑 | T3.1, T1.4 | B1/B2/B6 |
| T3.3 | 运行执行器 | `pipeline/runner.py`：`create_subprocess_exec`、流式读 stdout、更新 attempts、结束判定 | T3.2, T1.2 | FR-RUN-01/02/03 |
| T3.4 | 日志脱敏 | 命令回显与日志中 Key 脱敏 | T3.3 | S3/S4 |
| T3.5 | 轨迹解析与时间线 | 复用 `trace.py`（import + 降级），`GET /api/tasks/{id}/timeline` | T1.1 | FR-EVD-01/03 |
| T3.6 | diff 接口 | `GET /api/tasks/{id}/diff`（`git diff --numstat` + 单文件全量） | T1.2 | FR-EVD-02 |
| T3.7 | 轨迹 UI | 虚拟滚动时间线、搜索、只看失败、单条展开 | T0.5, T3.5 | 200 步滚动 60fps |
| T3.8 | 产物 UI | 文件列表 + diff 视图 | T0.5, T3.6 | 长 diff 横向滚动不破版 |
| T3.9 | 录屏封装 | `record list/plan/check-plan/start/stop` 的网关封装 + 串行锁 | T1.1 | FR-REC-08 |
| T3.10 | 录屏 UI | 计划表单、窗口选择、3-2-1 倒计时、计时器、校验结果 | T0.5, T3.9 | FR-REC-01..06 验收 |
| T3.11 | 任务详情页骨架 | 三栏布局、StepRail、Inspector、Tab 容器 | T0.5, T1.3 | 三栏可拖拽，1024 下自动折叠 |

### M4 反馈与导出（D11–D12）

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T4.1 | 题面编辑器 | CodeMirror 6 + 字数仪表 + 实时门禁（防抖 300ms 本地预检，安装时以技能为准） | T3.11 | FR-PRM-02/04/05 |
| T4.2 | 题面安装与锁定 | `prompt` 调用、errors 原样展示、安装后只读、重开本题 | T4.1 | FR-PRM-04/05/06 |
| T4.3 | 快照推送 UI | `repo` 调用、SHA/permalink 展示与复制 | T1.1, T3.11 | FR-REP-01/02/03 |
| T4.4 | Feedback 编辑器 | 模板初始化、编辑、保存即 `feedback --check`、4 个 Meter | T3.11 | FR-FBC-01/02/03 |
| T4.5 | 复核确认 | 勾选 + `review-ack.json` + 导出前置 | T4.4 | FR-FBC-04 |
| T4.6 | 单题导出 | `export` + 页面内预览 + 三按钮 | T4.5 | FR-EXP-01/03/04 |
| T4.7 | 批量导出 | `export-batch` + 跳过原因分组 | T4.6 | FR-EXP-02 |
| T4.8 | 看板 | 分组、指标条、进度条、实时计数 | T3.11 | FR-OPS-01/02 |
| T4.9 | 事件流 UI | 全局事件列表 + 筛选 | T1.4 | FR-OPS-03 |

### M5 打磨与验收（D13–D15）

| ID | 任务 | 产出 | 依赖 | 验收 |
|---|---|---|---|---|
| T5.1 | 全局快捷键 | `useHotkeys`：`g` 前缀、`⌘K`、`j/k`、`[/]`、`1–7` | T4.8 | `08` §4 全部按键可用 |
| T5.2 | 命令面板 | `⌘K`，任务/命令/页面三组 | T5.1 | 200 任务下输入无延迟 |
| T5.3 | 动效落地 | 照 `06` 实现 9 处白名单 | T4.8 | `06` §9 自检清单全绿 |
| T5.4 | 密度切换 | 两档密度 + 自动档 | T0.5 | `07` §4 |
| T5.5 | 空/错/载三态 | 全站补齐 | 全部 | 断网时每页都有明确文案与原文错误 |
| T5.6 | 批量操作 | 4 类批量 + 行级反馈 | T4.8 | `08` §6 |
| T5.7 | 设置页 | 6 组配置 + 连接测试 + 日志 | T0.2 | 改配置即时生效 |
| T5.8 | 端到端演练 | 完成 `09` §4 | 全部 | 演练脚本 9 步全通 |
| T5.9 | 安全自检 | `09` §8 的 S1–S7 | 全部 | 7 项全过 |

## 4. 每日检查点

| 日 | 结束时应能演示 |
|---|---|
| D2 | `localhost:8099` 打开一个空的工作台，导航可用 |
| D5 | **任务列表正确显示本机所有真实任务及 11 步状态** |
| D7 | 从工程台领取一个工程并建成任务 |
| D10 | 跑完一题，看到时间线、diff、录屏 |
| D12 | 导出第一份 xlsx，19 列齐全 |
| D15 | 端到端演练通过，快捷键与动效完成 |
| D17 | 真实试跑 2 天，8 题/天 |

## 5. 分阶段的可交付快照

每个里程碑结束打一个 tag（`m0`…`m5`），`git archive` 出一份 zip 存 `~/solo-auto/snapshots/`。
理由：开发出问题时可回退到「已知能跑」的状态，不必重头排查。

## 6. 风险登记

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | 技能 `state.json` 字段与文档不一致 | 中 | 高 | M1 先用真实文件验证；字段缺失走降级，不做硬依赖 |
| R2 | 工程台 Cookie 会话频繁过期，打断作业 | 中 | 中 | 只在领取/归还时用 Cookie；过期提示明确；支持「保持登录」后台续期 |
| R3 | ScreenCaptureKit 窗口 ID 变化导致录屏失败 | 中 | 高 | 复用技能已有 recorder；失败时保留原始错误并给「重选窗口」路径；不自行实现录制 |
| R4 | 单 worker 下长任务阻塞事件循环 | 低 | 高 | 所有子进程用 `asyncio.create_subprocess_exec`，禁止 `subprocess.run` 同步调用 |
| R5 | 并发跑 4 个容器导致本机资源耗尽 | 中 | 中 | 上限默认 4 可调；开跑前检查内存与磁盘；同 tick 只启 1 个 |
| R6 | 开发 Agent 自行「优化」需求（例如加自动写 Feedback） | 中 | **极高** | 本文档 + `01` §3 非目标 + `ADR-004` 三处明确禁止；实现后按 `09` §2.1 grep 检查 |
| R7 | 19 列字段顺序/表头与官方表不一致 | 低 | **极高** | 不手写表头，直接 `import spec.SUBMIT_COLUMNS`；导出后 openpyxl 读回比对 |
| R8 | 录屏期间用户操作不当导致画面无效 | 中 | 中 | 倒计时 + 「不要切换前台」提示 + cursor guard 校验 |
| R9 | 时间估算偏乐观（AI 生成 UI 慢） | 高 | 低 | UI 任务已按页面拆分，可延后到 M5 收尾；后端优先 |
| R10 | 真实试跑才发现流程缺环 | 中 | 高 | D12 后先做一次「模拟试跑」（用假数据走全流程），提前暴露 |

## 7. 开发时的五条硬规矩

交给执行开发的模型，这些是**不可协商**的：

1. **不实现 Feedback 生成、润色、评分。** 编辑器只能写、只能调 `feedback --check`。
   违反即整个交付作废（数据不允许返修）。
2. **不直接写技能目录。** 所有技能产物由技能自己写；控制台只写 `drafts/`。
3. **不硬编码 19 列表头。** `from spec import SUBMIT_COLUMNS`，导入失败则启动失败（宁可起不来）。
4. **不引入表外依赖。** 依赖清单见 `03` §2.3；要加库必须在 `12-open-questions.md` 登记理由。
5. **不做「强制通过」按钮。** 门禁不过就是不过，UI 上不存在绕过路径。

## 8. 完成定义（DoD）

一个任务算完成，必须同时满足：

- [ ] 代码合入，`make check` 全绿
- [ ] 对应测试用例存在且通过（`09` 中列出的编号）
- [ ] 界面三态（空/错/载）已处理
- [ ] 快捷键与焦点可见已实现
- [ ] 失败路径有原文错误展示
- [ ] 无 `TODO` 遗留（除明确登记在 `12` 的）
- [ ] 相关文档段落已同步（若实现改变了设计）
