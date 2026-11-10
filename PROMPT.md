# 启动提示词 · 交给开发模型

> 用法：在**项目根目录**（`solo-202611/solo-auto/`）新开一个会话，把下面 `====` 之间的内容整段粘贴过去。
> 不要一次性把 13 份文档塞进上下文 —— 提示词里已经规定了按里程碑分批读。

====

你在 `/Users/gaobo/repositories/gitlab/评审项目/solo-202611/solo-auto/` 下开发一个本地单机 Web 控制台，
名字叫 **solo-auto**（2611 作业台）。这个目录**目前只有文档，没有代码**，你要从零开始实现。

## 这个产品是什么

上一期有个技能 `sologsb-0917`（Pair-wise GSB）配一个监控台 `sg-auto`。这一期形态变了：

- 技能换成了 `solo-2611`（单轮 pointwise Feedback，纯 CLI，v0.2.2），已经存在且**不要改它**。
- 题目来源换成了本机「工程台 Project Hub」（`http://localhost:8088`），工程需要独占领取。
- 交付物是一份 **19 列的 Excel** + 轨迹 + 720p 录屏 + 250 字 Feedback。

你要做的是把「手工敲 11 条命令」变成「看板驱动的流水线」：调度、门禁、证据归集、导出。

## 第一步：先读文档，不要先写代码

按这个顺序读，**读完 01、02、13 再动手**：

| 顺序 | 文件 | 你要从里面拿到什么 |
|---|---|---|
| 1 | `docs/01-requirements.md` | 范围、非目标、FR 编号（验收会按编号查） |
| 2 | `docs/13-skill-cli-contract.md` | **技能的每个命令、参数、输出格式、退出码**。这是最重要的一份 |
| 3 | `docs/02-architecture-dataflow.md` | 分层、一次题目的完整生命周期、状态机、并发、断点恢复 |
| 4 | `docs/03-tech-stack.md` | 依赖清单（不许随便加库）、5 条 ADR |
| 5 | `docs/04-data-storage.md` | `~/solo-auto/` 布局、6 张表 DDL、状态映射表、对账算法 |
| 6 | `docs/10-dev-plan.md` | **你的任务清单**（M0–M5，49 个任务 T0.1–T5.x） |
| 7 | `docs/11-api-contract.md` | 你要实现的 HTTP/SSE 接口 |
| 8 | `docs/09-acceptance.md` | 怎么证明你做对了（逐条 FR 的验证方法） |
| 9 | `docs/05` `06` `07` `08` | 只在做前端时读，做后端可以跳过 |
| 10 | `docs/12-open-questions.md` | 有 4 个问题等人回答，**不要自己猜一个答案然后硬做** |

`docs/13-skill-cli-contract.md` 是我逐行读技能源码核对出来的，**与 `SKILL.md` 的措辞冲突时以它为准**。
里面标了 7 处「前序文档勘误」，别按旧说法实现。

## 五条硬规矩（违反即整个交付作废）

1. **不实现 Feedback 的生成、润色、改写、评分。** 你只能提供一个文本框和「调 `feedback --check`」的按钮。
   前端空文本框点保存就存空内容并让检查报错，**后端绝不补全一个字**。这一期的数据不允许返修，
   抽检不合格会牵连该专家全部历史数据，程序代写等于造假。

2. **不实现「自动给交付完整性打分」。** 1–5 分必须由看过轨迹和产物的人给出，程序只做存储与传递。

3. **不直接写技能的任务目录。** `delivery/提示词.md`、`feedback.md`、`交付表.xlsx`、`state.json`
   全部由技能自己写。你要编辑的内容放 `~/solo-auto/drafts/<taskId>/`，安装时用
   `prompt --file <你的草稿路径>` 交给技能。
   唯二例外：`monitor/runtime-verification.json` 与 `monitor/review-ack.json`（控制台独占，技能不认）。

4. **不硬编码 19 列表头。** 必须 `from spec import SUBMIT_COLUMNS`（技能目录加入 `sys.path`），
   导入失败就启动失败 —— 宁可起不来，也不要导出一份列顺序错位的表。

5. **不提供任何「强制通过」「跳过门禁」的入口。** 门禁不过在 UI 上就是不过，没有隐藏路径、没有 URL 参数。

另外：**不要「优化」需求**。如果你觉得某个设计不合理，写进 `docs/12-open-questions.md` 然后按原设计做，
不要自作主张换方案（比如自己想一个「更优雅」的并发模型）。

## 工作方式

### 按里程碑推进，每个里程碑停下来自检

```
M0 骨架        D1–D2    起服务、起页面、/api/health 通
M1 事实层      D3–D5    ★ 关键：只靠读磁盘就能正确呈现本机所有真实任务
M2 题源与建题  D6–D7    工程台适配、领取、ZIP、init
M3 运行与证据  D8–D10   队列、容器、轨迹、diff、录屏
M4 反馈与导出  D11–D12  Feedback 门禁、复核、xlsx
M5 打磨与验收  D13–D15  快捷键、动效、端到端演练
```

**M1 没做对之前不要碰 UI。** 事实层错了，界面再漂亮也是在展示错的数据。

### M1 的验收标准（这是整个项目的地基）

不写任何业务逻辑，仅靠扫描 `~/claude-runs/solo-2611/*/monitor/state.json`，
把该目录下**现有的全部真实任务**（当前 3 个，后续会增长）正确列出来并计算 11 步状态，
与 `python3 ~/.codex/skills/solo-2611/scripts/solo2611.py status --task-root <task>` 的输出逐项一致。

任务少反而更容易验证：逐个手工比对，任何一个字段不一致都算没通过。
另外要自己造 3 类假任务来补足覆盖面 —— 只有题面没跑的、运行中断的、已导出的 —— 放在
`tests/fixtures/tasks/` 下，不要往真实任务目录里写。

做到这一点再往下走。做不到就说明状态判定理解错了，后面全要返工。

### 每完成一个任务

- 跑 `make check`（ruff + mypy + tsc + eslint + 那 4 条 grep 检查）
- 补上 `docs/09-acceptance.md` 里对应的测试用例编号
- 失败路径必须有「原文错误展示」，不许写「操作失败，请重试」

## 特别容易做错的三件事（技能的实现细节）

### 1. `run` 的进度在 stderr，不在 stdout

技能的 `run` 把容器输出重定向到了 `monitor/runs/attempt-NN/stdout.jsonl`，
自己的 stdout **只在最后 emit 一次 JSON**。所以如果你只读 stdout，界面会表现为
「点运行 → 半小时没反应 → 突然完成」。

正确做法：同时读三个地方 —— 技能进程的 stderr（阶段行）、`attempt-NN/stdout.jsonl`（容器在干什么）、
`attempt-NN/stderr.log`（出错原因）。

### 2. 技能的容器上限会让它**静默阻塞**

`run` 内部有 `while 运行中 >= maxContainers: sleep(10)`，而且它先把 `status=running` 写进磁盘了。
所以如果你的队列上限比技能的 `maxContainers` 大，会出现「界面显示 N 个运行中，其实有一半在等名额」。

正确做法：**读技能 config 的 `claude.maxContainers` 作为你自己队列的唯一闸门**，两边用同一个数。

### 3. 退出码 1 和 2 含义不同

- **1** = 业务失败（前置条件不满足、网络挂了、仓库 403）→ 展示 stderr 原文 + 给修复动作
- **2** = **门禁未通过但参数没问题**（题面超字数、Feedback 不合格、导出缺列）→ 解析 stdout 的
  `errors` / `missing` 数组，逐条高亮

混成一种处理，用户就分不清「该去改配置」还是「该去改题面」。

## 技术选型（已定，不要换）

- 后端：Python 3.11 + FastAPI，**单 uvicorn worker**，`asyncio.create_subprocess_exec`
- 前端：React 19 + TypeScript + Vite + TanStack Query v5 + 原生 CSS（**不用 Tailwind**）
- 存储：SQLite（WAL）作索引，磁盘上的 `state.json` 是唯一事实源
- 依赖清单在 `docs/03-tech-stack.md` §2.3，**要加库先在 `12-open-questions.md` 登记理由**

不要引入：SQLAlchemy、Celery、Redis、Alembic、Redux、任何 UI 组件库、任何图表库。

## 开工前的第一步

1. 读 `01`、`13`、`02`。
2. 用 `python3 ~/.codex/skills/solo-2611/scripts/solo2611.py version` 确认技能在（应输出 `0.2.2`）。
3. 挑一个 `~/claude-runs/solo-2611/` 下已有的真实任务，`cat` 它的 `monitor/state.json`，
   对照 `13` §4 的字段表确认你理解了结构。
4. 然后按 `10-dev-plan.md` 从 T0.1 开始。

现在开始。第一步输出你对 `docs/13-skill-cli-contract.md` 的理解摘要（不超过 20 行），
说明你认为哪三条实现事实会影响你的架构，等我确认后再写代码。

====

## 使用说明（给你自己看，不用粘进去）

**为什么最后要求它先复述理解**：这个项目最大的风险不是写不出代码，而是开发模型按 `SKILL.md`
的措辞去猜参数，写出「能跑但结果不对」的网关。让它先复述，能在写第一行代码前暴露理解偏差，
比跑通之后返工便宜得多。

**如果它开始自己做设计决策**（比如「我觉得用 Redis 更合适」「我帮你把 Feedback 也自动生成了」）：
直接引用本文档「五条硬规矩」第 1、5 条和 `docs/03` 的依赖清单驳回。这三处是最常见的越界点。

**如果它想跳过 M1 直接做界面**：让它先把「列出本机所有真实任务且 11 步状态与 CLI 一致」跑通。
这一步不过，后面所有界面都是在展示可能错误的数据。

**如果它问 `12-open-questions.md` 里的 4 个问题**：先按文档里的「建议」执行并留扩展点，
不要停下来等 —— 这 4 项都有推荐方案，等回答会卡住进度。
