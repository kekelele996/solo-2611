# solo-auto · 2611 作业台

本地单机版 26.11 单轮 pointwise 作业控制台。当前为 **v0.3.0 调度中心**，围绕已有的 `solo-2611` v0.3.2 CLI
完成题源领取、建题、题面门禁、容器调度、证据阅读、窗口录屏、Feedback 复核和 19 列 Excel 导出。

v3 把生产线升级为一处统一调度：

- 「调度」页直接调整容器、会话、在手、启动间隔、弹性与目标，不再往返设置页；
- 容器、Docker、磁盘、题源、会话、录屏和技能异常统一表示为可探测、可解释、分范围的「挂起」；
- 队列与失败退避落入 SQLite，失败分类与重试策略不再依赖零散关键字；
- 每个未启动任务都显示门禁原因，任意页面顶栏都能看到调度状态和一键启停；
- `dispatch.engine = "v2"` 可整套退回原生产线，迁移不删 v2 配置或任务数据。

## 边界

- 程序不生成、润色或改写 Feedback，也不自动填写交付完整性分数。
- `state.json`、题面、交付表和核验记录仍由技能或人工产生；控制台管理自己的索引与显式复核记录。
- 19 列表头在启动时从技能 `spec.SUBMIT_COLUMNS` 导入，导入失败则拒绝启动。
- 服务只绑定 `127.0.0.1:8099`，单 uvicorn worker。
- SOLO2 自动提交默认开启：导出通过门禁后直接上传轨迹和录屏、提交并轮询质检；`dry-run` 只用于排障。
- v3 以平台调度上限为唯一编辑入口，写回技能 `claude.maxContainers` 并回读校验；Docker 探测失败视为「未知」，绝不按 0 占用继续启动。

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
4. 打开「调度」页确认容器上限、会话并发、在手上限与启动间隔；选择「均衡」预设即可开始。
5. 从“题源”选择一个项目；未选择项目时不会请求工程列表。

## 验证

```bash
make check
```

包含后端 Ruff、核心模块 `mypy --strict`、Pytest、OpenAPI 类型生成、TypeScript、
ESLint、Vitest、Vite 构建及硬性 grep 检查。

验收夹具位于 `backend/tests/fixtures/tasks/`，覆盖待出题、运行中断和完整导出三类任务；
`backend/tests/test_dispatch.py` 另覆盖配置迁移、Docker 未知、挂起范围、队列持久化、失败退避与调度 API。

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

完整设计与验收口径见 `PROMPT.md`、`docs/v3/` 和 `docs/v3/implementation-status.md`。
