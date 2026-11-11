# 03 · 技术选型

## 1. 选型原则

1. **不重复造执行层。** 容器、录屏、Excel、GitHub 推送全部由现有技能负责，控制台只调用。
2. **单机优先。** 不引入需要运维的中间件（消息队列、Redis、独立数据库服务）。
3. **状态可 `cat`。** 任何时刻，用户不打开控制台也能靠命令行还原全貌。
4. **前端要能读长文本。** 轨迹时间线、diff、Feedback 都是长内容，需要好的阅读体验，
   这是本期与上一期的最大差异 —— 上一期以「卡片状态」为主，本期以「读证据」为主。

第 4 条决定了前端不该再用「原生 JS + 单 HTML 文件」。上一期页面主体是 4 个列表页，
本期需要编辑器（题面、json 表单）、虚拟滚动（时间线可能有 200+ 步）、diff 高亮、字数仪表、
录音计时器。手写这些的维护成本会超过引入框架的成本。

## 2. 后端

### 2.1 主选：Python 3.12 + FastAPI

| 维度 | 说明 |
|---|---|
| 语言一致性 | 技能是 Python，控制台可以 `sys.path` 复用 `spec.py` 的枚举、`excel.py` 的读回校验、`trace.py` 的解析函数，避免两套字段定义 |
| 异步能力 | 运行/录屏/导出都是长时间子进程，需要 `asyncio.create_subprocess_exec` + 流式读 |
| 契约生成 | 自动产出 OpenAPI，前端可用 `openapi-typescript` 生成类型，减少前后端字段漂移 |
| SSE | `StreamingResponse` 直接实现，不需要额外依赖 |

**为什么不用 Django**：太重，ORM/迁移/管理后台都不需要；单机单表用不上。

**为什么不用 Flask**：异步子进程与流式响应要额外引入扩展，FastAPI 原生更省事。

**为什么不用 `http.server`（上一期的做法）**：上一期只有 4 个列表页 + 4 个写接口，
手写路由尚可；本期有 25+ 接口、需要 Pydantic 校验、需要 SSE、需要后台任务。
用标准库会把「校验」「生命周期」「并发」全部手写一遍，出错面更大。

### 2.2 进程模型

```
uvicorn（单 worker，asyncio）
 ├── HTTP 路由
 ├── 后台协程：scheduler.tick()      每 2s
 ├── 后台协程：state_watcher()       文件变更即刷新
 ├── 后台协程：reconcile()           每 60s 对账磁盘与索引
 └── 子进程池：SkillGateway 启动的 solo2611.py
```

**单 worker 是刻意的**：状态在内存 + 文件锁，多 worker 会让队列出现竞态。单机单用户的吞吐
瓶颈在容器运行（25–60 分钟/题），不在 HTTP 层。

### 2.3 依赖清单（尽量短）

```
fastapi==0.115.*
uvicorn[standard]==0.32.*
pydantic==2.9.*
pydantic-settings==2.6.*
httpx==0.27.*          # 访问工程台，支持流式下载 ZIP
watchdog==5.0.*        # state.json 变更监视
openpyxl==3.1.*        # 仅在需要读回校验时使用（技能已依赖）
```

不引入：SQLAlchemy（手写 SQL 更透明，表只有 6 张）、Celery（无分布式需求）、Redis（同上）、
Alembic（单表结构手工迁移足够）。

## 3. 前端

### 3.1 主选：React 19 + TypeScript + Vite

| 维度 | 说明 |
|---|---|
| 组件生态 | 长文本编辑器、虚拟列表、diff 视图都有成熟方案 |
| 类型安全 | 与后端 OpenAPI 生成类型对齐，字段改名会在编译期报错 |
| 构建 | Vite 启动 < 1s，构建产物静态托管，不需要 Node 服务常驻 |

### 3.2 具体库

| 用途 | 选型 | 理由 |
|---|---|---|
| 路由 | React Router 7（data mode） | 页面不多，不需要框架级 SSR |
| 数据请求 | TanStack Query v5 | 轮询、缓存失效、乐观更新都是现成的；SSE 只用来「通知刷新」 |
| 样式 | **原生 CSS + CSS 变量**，配 `PostCSS` | 见 `05-ui-design.md`，这个产品需要精确的密度控制，Tailwind 的默认间距不适用 |
| 组件 | 自建（约 18 个基础组件） | 避免引入整套设计语言导致「一眼 AI 味」 |
| 编辑器 | CodeMirror 6（Markdown 模式） | 比 textarea 多了行号、软换行、搜索；比 Monaco 轻 10 倍 |
| JSON 表单 | 自建 schema 驱动表单 | 只有两份 JSON 需要编辑，字段固定 |
| 时间线/虚拟滚动 | `@tanstack/react-virtual` | 200+ 步的轨迹必须虚拟化 |
| diff | `diff` + 自绘行级高亮 | 不需要完整 diff 编辑器 |
| 图表 | 自绘 SVG（微型条形/仪表） | 只有字数、锚点数等 4 个指标，引入图表库不值 |
| 图标 | Lucide（按需 import） | 线性风格，与密集界面匹配 |
| 日期 | `date-fns` | 体积小，不需要时区库 |

### 3.3 状态分层

```
服务器状态  → TanStack Query（唯一，不做本地镜像）
UI 局部状态  → useState / useReducer
跨页选择态  → Zustand（仅 3 个 store：筛选、批量选择、录屏向导步骤）
URL 状态    → 筛选条件、当前任务、当前 tab 全部进 query string（可分享/可刷新）
```

**不引入 Redux**：没有跨页复杂业务状态。

## 4. 存储

| 用途 | 选型 | 理由 |
|---|---|---|
| 事实源 | 文件系统（技能的任务目录） | 技能写的就是它，不另存一份 |
| 索引 | SQLite（WAL 模式） | 单文件、零运维、支持并发读；20–200 题规模完全够 |
| 配置 | TOML + macOS Keychain | TOML 手改友好；密钥不进文件 |
| 缓存 | 内存 LRU | 进程内即可，重启重建 |

详见 `04-data-storage.md`。

## 5. 与工程台的集成方式

| 方案 | 结论 |
|---|---|
| 直接读工程台的 PostgreSQL | ❌ 耦合其内部 schema，版本一变就崩；且违反其 API 契约 |
| 浏览器自动化点页面 | ❌ 脆弱，且领取是写操作，出问题难排查 |
| **调用其 HTTP API** | ✅ 官方提供 `/open/v1` 与 `/api/v1`，有 OpenAPI 文档，鉴权清晰 |

认证策略：

- **浏览/筛选**：Bearer 令牌（普通成员，只读）—— 无需维护 Cookie。
- **领取/归还**：Cookie 会话（用户名密码登录，带 `X-CSRF-Token`）—— 必需，因为 Bearer 成员令牌只读。
- 两者都存 Keychain；Cookie 过期时提示重新登录，不静默降级。

**为什么不缓存工程数据**：工程台是唯一权威，缓存会引入「我这边显示可领取、实际已被别人领走」的错觉。
只缓存**已领取工程的快照**（id、名称、zip_sha256、领取时间），用于离线查看。

## 6. 关键取舍记录（ADR）

### ADR-001 · 控制台不直接读技能内部函数，只调 CLI

- **决策**：通过 `subprocess` 调 `solo2611.py`，不 `import` 其模块修改状态。
- **理由**：技能有自己的锁、状态原子写入、日志约定。进程内调用会绕过这些，且技能升级时冲突。
- **代价**：每次调用有 ~200ms Python 启动开销，可接受。
- **例外**：只读的解析函数（`trace.analyze`、`spec.SUBMIT_COLUMNS`）允许直接 import，
  但必须 try/except ImportError，失败时降级为「自己解析」。

### ADR-002 · 不引入后台任务框架

- **决策**：用 `asyncio.Task` + 内存任务表 + 磁盘状态。
- **理由**：任务生命周期与 HTTP 请求解耦即可，不需要重试策略、结果后端、定时调度。
- **风险**：进程重启后内存任务丢失 → 由 `reconcile()` 从磁盘恢复（见 `02` §6）。

### ADR-003 · 前端不打包成单文件，静态托管

- **决策**：Vite 构建产物由 FastAPI 的 `StaticFiles` 托管，同端口。
- **理由**：避免 CORS、避免第二个进程、避免端口管理。开发期用 Vite proxy。

### ADR-004 · 不做「一键全自动跑 N 题」

> v2 修订：ADR-004 已被 v2 ADR-006 取代。自动交付与生产计划仍保留，但必须配合抽检、熔断和 `ackedBy=autopilot` 标识，见 `docs/v2/02-design.md`。

- **决策**：出题与 Feedback 永远需要人/Agent 介入，控制台最多做「批量排队：已出好题的任务」。
- **理由**：本期数据不允许返修，抽检不合格会牵连全部历史。自动选题 + 自动出题 + 自动评价
  等于把质量完全交给机器，一旦规则理解偏差就是批量废数。
- **折中**：允许「批量排队已 ready 的任务」，允许「批量检查门禁」，但不允许跳过人工复核。

### ADR-005 · 录屏由控制台驱动、技能执行

- **决策**：窗口枚举、计划校验、起停、证据读取都走技能 `record` 子命令；控制台只做 UI 与串行锁。
- **理由**：ScreenCaptureKit 的 Swift 录制器已在技能里，且经过上一期验证（窗口 ID、cursor guard）。
  重写一遍没有收益，反而可能丢证据格式。

## 7. 开发环境

```bash
# 后端
cd backend && python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
uvicorn solo_auto.app:app --reload --port 8099

# 前端
cd frontend && npm ci && npm run dev     # 5173，proxy 到 8099
```

CI（本地 `make check`）：`ruff` + `mypy --strict`（核心模块）+ `pytest` + `tsc --noEmit` + `vitest`。

## 8. 版本与发布

- 控制台自身版本语义化；`GET /api/version` 返回版本、git 短 SHA、技能版本、技能 `schema`。
- 打包：`python -m build` 出 wheel；`make app` 出 `.app`（可选，用 py2app 或简单 shell 启动器）。
- 首版只支持「源码目录 + 一条启动脚本」，不追求安装包。
