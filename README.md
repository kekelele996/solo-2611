# solo-auto · 2611 作业台

本地单机版 26.11 pointwise 作业控制台。当前为 **v0.4.0 受管会话版**，围绕 `solo-2611` v0.7.1 CLI
完成题源领取、建题、题面门禁、容器调度、证据阅读、窗口录屏、Feedback 复核和 19 列 Excel 导出。

受管会话采用“发起 + 轮询”：后端只负责启动技能监督器并读取 `run-status`，不再依赖一条会因重启而断开的
子进程连接。任务页显示轮次、继续次数、等待原因和倒计时；中断后在同一容器内继续，继续用尽或超时才终止并换题。

v3 把生产线升级为一处统一调度：

- 「调度」页直接切换“容器优先 / 任务优先”，并调整容器上限、任务上限、建题冷却和目标；
- 调度页固定展示「待处理 / 正在处理 / 已处理」三列，待处理最多预览下一批 20 道工程；
- 容器、Docker、磁盘、题源、会话、录屏和技能异常统一表示为可探测、可解释、分范围的「挂起」；
- 队列与失败记录落入 SQLite；同一题累计失败达到上限才放弃，自动回收并补位；
- 每个未启动任务都显示门禁原因，任意页面顶栏都能看到调度状态和一键启停；
- `dispatch.engine = "v2"` 可整套退回原生产线，迁移不删 v2 配置或任务数据。

## 边界

- 程序不生成、润色或改写 Feedback，也不自动填写交付完整性分数。
- `state.json`、题面、交付表和核验记录仍由技能或人工产生；控制台管理自己的索引与显式复核记录。
  Feedback 与轨迹总体意见冲突时，工作台会停在待核验状态，不再自动拉 Agent 改写后硬提交。
- 19 列表头在启动时从技能 `spec.SUBMIT_COLUMNS` 导入，导入失败则拒绝启动。
- 服务只绑定 `127.0.0.1:8099`，单 uvicorn worker。
- SOLO2 自动提交默认开启：导出通过门禁后直接上传轨迹和录屏、提交并轮询质检；`dry-run` 只用于排障。
- v3 以平台调度上限为唯一编辑入口，每分钟写回并回读技能 `claude.maxContainers`；Docker 实况同时识别
  `solo2611=true` 标签和 `solo2611-*` 名称前缀，探测失败视为「未知」，绝不按 0 占用继续启动。
- 终态失败题默认短暂等待后自动清理到回收站、归还工程并立即触发补位；已导出或已提交 SOLO2 的题保留现场。
- Claude 启动调用有 300 秒容错窗口，返回异常时会先检查已有容器、tmux 和监督器，能接管就恢复，不重复占名额。
- 可见受管终端由 attach 适配器自行关窗，工作台每分钟再清扫一次旧版本残留。

## 环境

- macOS、Docker Desktop、Swift/Xcode CLT
- Python 3.12+
- Node.js 20+
- 已安装并配置 `~/.codex/skills/solo-2611`
- 工程台默认地址 `http://localhost:8088`

## 安装

```bash
make setup
```

默认数据目录是 `~/solo-auto`。首次启动会写入 `config.toml`、SQLite 索引和日志。
可用 `SOLO_AUTO_HOME` 指向隔离目录。

## 启动

```bash
make dev
```

打开 <http://127.0.0.1:8099>。前端生产资源由 FastAPI 同端口托管。

开发模式可分别运行：

```bash
cd backend
SOLO_AUTO_HOME="$HOME/solo-auto" .venv/bin/python -m solo_auto

cd frontend
npm run dev
```

Vite 开发页是 <http://127.0.0.1:5173>，`/api` 代理到 8099。

## 首次配置

1. 设置页确认技能路径、任务目录和工程台地址。
2. 登录工程台，密码保存在 macOS Keychain，`secrets.json` 仅为 0600 回退。
3. 在设置页测试 Skill、Project Hub 和 Docker。
4. 打开「调度」页选择调度模式：
   - 容器优先：容器占用达到上限才停建题，在手任务数可浮动；
   - 任务优先：在手任务达到最大任务数才停建题，超额任务在容器门禁排队。
   两种模式都受严格建题冷却约束。
5. 从“题源”选择一个项目；未选择项目时不会请求工程列表。“工程列表 / 提示词库”两个页签分别承载领取与
   历史题面；提示词库按具体工程归档，可按工程或题面搜索、复制，并以 JSON/JSONL 导入或导出备份。

新建任务会在 `monitor/workbench.json` 写入项目与工程范围。`solo-2611` 的
`prompt-history` 和 `prompt --init` 可据此读取同工程历史，安装题面前再调用去重查询；
典型用法见技能目录中的 `solo-2611/SKILL.md`。

## 并发与配置热更新

- `[dispatch] mode = "containers"` 时，建题门禁只看 Docker 容器实况：即将超过 `containers` 才停建题，`max_tasks` 不参与判断。
- `[dispatch] mode = "tasks"` 时，先按 `max_tasks` 控制在手任务数；多出来的任务仍要等待 `containers` 容器名额。
- `create_cooldown_seconds` 是严格建题冷却，即使容器和任务都空着也不能提前结束。
- Docker 探测失败按“未知”处理，不能当成 0 容器继续创建任务。
- 终态失败题自动移入回收站、归还工程并触发补位；已导出或已提交 SOLO2 的题保留现场。
- 受管会话总时限默认 3 小时。后端通过 `run-status` 接管现有会话；运行中断后不重新发送题面，而是从状态文件和事件日志恢复。
- 可见终端由自回收适配器在 tmux 退出后关闭，工作台每分钟再清扫残留；仍在运行的会话和用户自己的终端不会被关闭。
- 直接修改 `~/solo-auto/config.toml` 后约 5 秒自动生效，不用重启。调大上限会逐步补位，调小上限不打断正在运行的任务。写错的 `[dispatch]` 会记录 `config.reload_failed`。

## 验证

```bash
make check
```

包含后端 Ruff、核心模块 `mypy --strict`、Pytest、OpenAPI 类型生成、TypeScript、
ESLint、Vitest、Vite 构建及硬性 grep 检查。

验收夹具位于 `backend/tests/fixtures/tasks/`，覆盖待出题、运行中断和完整导出三类任务；
`backend/tests/test_creation_gate.py` 覆盖容器优先、任务优先、严格冷却和 Docker 未知；
`backend/tests/test_session_v4.py` 与 `test_auto_recovery.py` 覆盖受管会话接管、恢复和终端回收。

Linux 核心链路（不含 ScreenCaptureKit）可用 Docker 验证：

```bash
make test-linux
```

## 目录

```text
backend/
  solo_auto/
    dispatch/            v3 调度内核、事实快照、挂起、门禁、分类、策略与探测
    pipeline/            v2 阶段执行器与兼容路径
  tests/                 单元、集成、API 与三类任务夹具
frontend/
  src/pages/
    DispatchPage.tsx     调度中心
    DispatchLogPage.tsx  调度日志
docs/                    需求、架构、契约与验收文档
config.example.toml      配置模板
```

完整设计与验收口径见 `PROMPT.md`、`docs/v3/`、`docs/v4/` 和 `docs/v4/implementation-status.md`。
