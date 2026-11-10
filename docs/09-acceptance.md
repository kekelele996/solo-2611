# 09 · 验收方案

## 1. 验收的对象是什么

不是「界面好看」，也不是「功能都有」，而是四件事：

1. **不造假**：所有提交字段都能追溯到磁盘上的一份真实证据。
2. **不漏项**：19 列没有一格是猜的、填默认值糊弄的。
3. **不静默失败**：任何异常都会停下来并说出原因，不会「看起来跑完了」。
4. **可恢复**：断电、重启、容器被杀之后，状态仍然准确。

因此验收分成四层：**静态检查 → 单元/集成测试 → 端到端演练 → 真实试跑**。
前三层在开发期完成，第四层是最终放行条件。

## 2. 第一层：静态检查（每次提交）

```bash
make check
  ├─ ruff check backend/             # 无未使用导入、无裸 except
  ├─ mypy --strict backend/solo_auto/{pipeline,gates,gateway,store}
  ├─ tsc --noEmit                    # 前端类型
  ├─ eslint frontend/src            # 无 any、无 console.log
  └─ grep 检查（见下）
```

### 2.1 硬性 grep 检查

```bash
# 1. 不允许在任何地方拼接 Feedback 正文（防程序代写）
! grep -rn "feedback.*write\|feedback.*generate\|polish" backend/ --include=*.py

# 2. 不允许直接写技能产物目录
! grep -rn "delivery/feedback.md\|delivery/提示词.md" backend/ --include=*.py | grep -v "read"

# 3. 前端不允许 outline: none
! grep -rn "outline: *none" frontend/src

# 4. 除白名单外不允许 transition/animation
grep -rn "transition:\|animation:" frontend/src | wc -l   # 必须 ≤ 12
```

### 2.2 类型与契约

- 后端所有技能调用返回必须经 `pydantic` 模型校验；解析失败必须是**显式异常**，不是 `None`。
- 前端所有 API 类型由 `openapi-typescript` 从后端 schema 生成，**不允许手写接口类型**。
  检查方式：`npm run gen:api && git diff --exit-code src/api/schema.ts`。

## 3. 第二层：单元与集成测试

### 3.1 覆盖目标

| 模块 | 覆盖率 | 理由 |
|---|---|---|
| `pipeline/steps.py` | 100% | 11 步判定是全系统的事实基础，错了全盘错 |
| `pipeline/scheduler.py` | ≥ 90% | 并发与队列是出错最贵的地方 |
| `gates/*` | 100% | 门禁是唯一的质量防线 |
| `store/repo.py` | ≥ 85% | SQL 与对账逻辑 |
| `sources/project_hub.py` | ≥ 80% | 用 `httpx.MockTransport` 模拟服务端 |
| `api/*` | ≥ 70% | 主要是参数校验 |
| 前端核心组件（StepRail / Meter / TrajectoryList） | ≥ 70% | 用 vitest + testing-library |

**覆盖率不是目的，未覆盖的分支必须有注释说明为什么不需要测。**

### 3.2 必测用例清单（不可省略）

#### A. 状态映射（`steps.py` / 对账）

| # | 输入 | 期望 |
|---|---|---|
| A1 | `state.json` 缺失 | 任务标记 `orphan`，步骤全为 `—`，不抛异常 |
| A2 | `schema` = `solo-2611/2`（未知版本） | 只读模式，横幅提示，不解析字段 |
| A3 | `status=running` 但对应 pid 不存在 | 判为 `stale`，提示「疑似中断」 |
| A4 | `status=running` 且进程存活 | 判为 `running`，显示已用时长 |
| A5 | `session.sessionId` 为 36 位 UUID | 步骤 4 通过 |
| A6 | `sessionId` 与库中另一任务重复 | 步骤 4 显示「重复」并阻止导出 |
| A7 | `initialSha` 长度 7 | 步骤 1 不通过（必须 40 位） |
| A8 | `artifact.sha == initialSha` | 步骤 6 不通过（产物未变化） |
| A9 | 录屏文件存在但 91 秒 | 步骤 9 不通过，原因「超 90 秒」 |
| A10 | 录屏 1280×718 | 步骤 9 不通过，原因「分辨率不符」 |
| A11 | `feedback-check.json` 缺失 | 步骤 10 不通过 |
| A12 | `feedback ok` 但未勾选复核 | 步骤 10 不通过，原因「未复核」 |
| A13 | `export.missing == []` | 步骤 11 通过 |

#### B. 并发与锁

| # | 场景 | 期望 |
|---|---|---|
| B1 | 并发上限 2，入队 5 题 | 同时最多 2 个进程；其余排队；顺序按入队时间 |
| B2 | 同一任务连点两次「运行」 | 第二次被拒，返回「已在运行中」 |
| B3 | 占位文件中 pid 已死 | tick 时回收占位，不减少可用槽位 |
| B4 | 两个控制台实例启动（同一 HOME） | 第二个拒绝启动，提示已有实例 |
| B5 | 录屏进行中点另一题录屏 | 被拒，提示「另一个录制进行中：gb-xxx」 |
| B6 | 401 返回 | 立即清空队列并置所有 queued 为 blocked，不再尝试 |

#### C. 题源适配

| # | 场景 | 期望 |
|---|---|---|
| C1 | 未选项目直接访问工程列表 | 404 / 空，界面提示「请先选择项目」 |
| C2 | ZIP 下载中途断开 | 重试 2 次后失败，不留半个 zip |
| C3 | zip sha256 与服务端不符 | 删除文件，报错并给出两边 sha |
| C4 | 工程已被他人领取（409） | 提示并从列表移除该行，不静默 |
| C5 | Cookie 过期（401） | 提示重新登录，不自动用 Bearer 降级 |
| C6 | 题源不可达 | 进入离线模式，禁止新建，已有任务可读 |
| C7 | 归还时 claim 已过期（404） | 提示「已被管理员回收」，本地标记释放 |

#### D. 门禁

| # | 场景 | 期望 |
|---|---|---|
| D1 | 提交 241 字的题面 | `prompt` 返回非 0，errors 原文展示，按钮禁用 |
| D2 | Feedback 179 字 | 检查不通过，Meter 显示红色 |
| D3 | Feedback 361 字 | 检查不通过 |
| D4 | Feedback 里含 rubric 原句 | 检查返回「照抄评分标准」类错误 |
| D5 | 锚点引用不存在的路径 | 检查返回锚点无效 |

#### E. 断点恢复

| # | 场景 | 期望 |
|---|---|---|
| E1 | 运行中 `kill -9` 控制台，重启 | 10 秒内列表状态正确；运行中的任务由 `docker ps` 判定存活 |
| E2 | 重启电脑后启动 | 所有 running 转为 `run_failed(host_restart)`，给出 reset 提示 |
| E3 | 删除 `solo-auto.db` 后启动 | 全量重建，任务数与磁盘一致 |
| E4 | 删除某任务的 `state.json` | 该任务标 `orphan`，其余不受影响 |

### 3.3 技能调用的 Mock 契约

所有测试**不执行真实 `solo2611.py`**，而是用一个 `FakeSkill`：

```python
class FakeSkill:
    def run(self, argv: list[str]) -> SkillResult:
        # 从 fixtures/<command>.json 读预置 stdout / exit_code
        # 并把 argv 记录到 self.calls，供断言
```

预置 fixtures 必须包含：成功、`ok=false`、退出码非 0 且 stderr 有内容、stdout 为空、stdout 是非法 JSON —— 五种。

**唯一允许真跑技能的测试**：`tests/e2e/test_real_skill_smoke.py`，标记 `@pytest.mark.slow`，
默认跳过，CI 不跑，发布前手动跑一次。

## 4. 第三层：端到端演练

用真实环境（真实 Docker、真技能、真题源）走完一题。**这是发布前的强制门槛。**

### 4.1 演练脚本（人工 + 半自动）

```
0. 前置：docker 可跑、solo-2611 已配置、工程台可达
1. 领取一个测试工程（用专门的「演练用」项目，产物不进入交付表）
2. 建任务 → 装题面 → 推快照
3. 运行（或用 interactive 手动跑一个简单任务缩短等待）
4. 读轨迹 Tab：时间线渲染、搜索、只看失败
5. 读产物 Tab：diff 统计与文件列表正确
6. 录屏：选窗口 → 倒计时 → 录 30 秒 → 停止 → 校验通过
7. Feedback：写 200 字 → 检查通过 → 勾选复核
8. 导出：11 项全绿 → 生成 xlsx → 在 Excel 打开肉眼核对 19 列
9. 归档 + 归还
```

### 4.2 演练的核对点

| 核对 | 方法 |
|---|---|
| SessionID 与轨迹文件内一致 | `grep sessionId traces/*.jsonl` 对比界面值 |
| PromptID 存在 | 界面值在 jsonl 中可搜到 |
| 初始快照是产物快照祖先 | `git merge-base --is-ancestor <initial> <artifact>` |
| 录屏能被 QuickTime 打开且 720p | 手动打开，看「显示简介」 |
| xlsx 表头 19 列顺序与官方表一致 | 与 `spec.SUBMIT_COLUMNS` 逐列比对 |
| xlsx 无合并单元格错位 | openpyxl 读回 + 目视 |
| 导出后再导出不产生重复行 | 连续导出两次，比对行数 |

### 4.3 失败演练（主动制造）

| 制造 | 期望 |
|---|---|
| 修改 `ANTHROPIC_AUTH_TOKEN` 为无效值 | 运行失败，归类 auth，队列熔断，顶栏红条 |
| 断开网络后点「领取」 | 网络错误原文，不进入暂存区 |
| 录制时切到别的窗口 | cursor guard / 前台变化报告记录，校验提示 |
| 运行中 `docker stop` 容器 | 归类为容器退出，提示可重跑 |
| 手工把题面改成 300 字后点安装 | 门禁拒绝 |

## 5. 第四层：真实试跑（放行条件）

连续 2 个工作日，按真实节奏作业，不做任何人工干预（除正常操作）。

### 5.1 放行门槛

| 指标 | 门槛 | 不达标怎么办 |
|---|---|---|
| 完成题数 | ≥ 8 题/天 | 检查是否有可自动化的步骤被漏掉 |
| 导出被自身检查打回 | 0 次 | 说明 11 项检查有漏洞，立即补 |
| 字段缺失 | 0 处 | 逐格核对，补判定函数 |
| 状态与实际不符 | 0 次 | 对账逻辑有 bug |
| 需要手敲技能命令 | 0 次 | 有功能没做全 |
| 崩溃/需重启 | ≤ 1 次/天 | 看 `app.log` |
| 单题耗时（不含写题与写反馈） | ≤ 25 分钟 | 分析时间分布 |

### 5.2 试跑期间的记录

新建 `docs/trial-YYYYMMDD.md`，每完成一题记一行：

```
| 任务 | 领取→建题 | 写题面 | 运行 | 读轨迹 | 录屏 | 写反馈 | 导出 | 卡在哪 | 备注 |
```

这张表是后续迭代的唯一依据，比任何用户访谈都准。

## 6. 逐需求验收表

逐条对照 `01-requirements.md`，每条给出「如何证明」。

### 6.1 题源（SRC）

| 编号 | 证明方式 |
|---|---|
| FR-SRC-01 | 在设置页把 base URL 改为 `http://127.0.0.1:8088` 后能正常列出项目 |
| FR-SRC-02 | 清空项目选择后，工程区为空且提示「请先选择项目」；直接调 API 返回 400 |
| FR-SRC-03 | 列表显示 5 个字段，与服务端 API 返回的字段逐一对应 |
| FR-SRC-04 | 在界面输入 `language=go` 后，抓包确认请求携带 `language=go`（本地无过滤代码） |
| FR-SRC-05 | 点领取先出现 Modal，取消则不发起请求 |
| FR-SRC-06 | 领取后 `staging/<eid>/source.zip` 存在且 `shasum -a 256` 与服务端一致 |
| FR-SRC-07 | 放弃本题后工程台侧 `scope=available` 重新出现该工程 |
| FR-SRC-08 | 领取后杀掉控制台，重启后「待开工」区仍有该项目 |
| FR-SRC-09 | `docker stop project-hub-api-1` 后界面进入离线模式，新建按钮禁用 |
| FR-SRC-10 | `sources/base.py` 为抽象基类，`local_dir.py` 可独立实现并通过同一组测试 |

### 6.2 题面（PRM）

| 编号 | 证明方式 |
|---|---|
| FR-PRM-01 | 建任务后 `taskRoot` 目录存在且 git 已初始化（`git -C ... rev-parse HEAD` 返回 SHA） |
| FR-PRM-02 | 输入 171 字时字数条变黄；输入 241 字时变红且按钮禁用 |
| FR-PRM-03 | 表单保存后 `monitor/prompt/difficulty-review.json` 内容与表单一致 |
| FR-PRM-04 | 故意写违规题面，页面显示的 errors 文本与命令行运行时的 stderr 完全一致（逐字比对） |
| FR-PRM-05 | `grep -r "force" frontend/src` 无「强制通过」按钮；门禁不过时按钮无任何可点击路径 |
| FR-PRM-06 | 安装后编辑器 `readonly`；「重开本题」必须填原因才可提交 |
| FR-PRM-07 | 对两条已知相似的历史题面，输入后出现警告并给出相似片段 |
| FR-PRM-08 | `⌘O` 能在 Finder 中打开，且路径与右栏显示一致 |

### 6.3 快照（REP）

| 编号 | 证明方式 |
|---|---|
| FR-REP-01 | 建任务后看板显示「待推送快照」，点击后 `state.remote.initialPushed` 出现 |
| FR-REP-02 | 界面 SHA 复制后与 `git rev-parse HEAD` 一致，长度 40 |
| FR-REP-03 | 用无效 token，页面展示 GitHub 原始错误（含 403 文本） |
| FR-REP-04 | 运行后右栏显示产物 SHA、permalink、`+N −M` |

### 6.4 运行（RUN）

| 编号 | 证明方式 |
|---|---|
| FR-RUN-01 | 运行中页面每 2s 刷新一次 stdout 尾部，内容与 `logs/skill/<id>/run-*.log` 一致 |
| FR-RUN-02 | 显示 `attempt 2/3` 与阶段名，阶段切换与日志中的关键字对应 |
| FR-RUN-03 | 制造超时后页面给出「按规范重 roll」的明确建议文案 |
| FR-RUN-04 | 上限设为 1，入队 3 题，只有 1 个容器在跑，其余显示队位 |
| FR-RUN-05 | 双击运行按钮，第三次请求返回 409 |
| FR-RUN-06 | `interactive` 模式生成命令可粘贴执行；结束后「收集」按钮拉回轨迹 |
| FR-RUN-07 | 无效 Key 时 5 秒内整个队列停摆，无第 2 次尝试 |
| FR-RUN-08 | 运行超 90 分钟后该行变 warn 色，但进程仍在 |

### 6.5 证据（EVD）

| 编号 | 证明方式 |
|---|---|
| FR-EVD-01 | 时间线步骤数 = jsonl 中 tool_use 条数（抽一题人工比对） |
| FR-EVD-02 | diff 文件数与 `git diff --stat` 行数一致 |
| FR-EVD-03 | 结构校验显示的 user 消息数与 jsonl 中 `role=user` 且非 tool_result 的条数一致 |
| FR-EVD-04 | 填 `pytest -q` 后输出与退出码出现在 `monitor/runtime-verification.json` |
| FR-EVD-05 | `⌘⇧C` 复制的路径能被 `open` 打开 |

### 6.6 录屏（REC）

| 编号 | 证明方式 |
|---|---|
| FR-REC-01 | 表单保存后 `record-plan.json` 与表单一致；`check-plan` 的 ok 反映在按钮状态 |
| FR-REC-02 | `record list` 返回的窗口数与界面列表一致；未选窗口时开始按钮禁用 |
| FR-REC-03 | 点开始后出现 3-2-1，倒计时期间无法操作其他按钮 |
| FR-REC-04 | 录制中计时每秒递增，到 85 秒变黄，90 秒自动停止 |
| FR-REC-05 | 停止后 2 秒内显示分辨率/时长/采集方式四项 |
| FR-REC-06 | Web 题未 `chrome-start` 时按钮禁用并提示 |
| FR-REC-07 | 手工放一个 1080p 视频进去，校验不通过且无「导入」按钮 |
| FR-REC-08 | 两题同时点录屏，第二个被拒并显示正在录的是哪题 |

### 6.7 Feedback 与导出（FBC / EXP）

| 编号 | 证明方式 |
|---|---|
| FR-FBC-01 | `feedback --init` 后模板含过程/产物两段骨架 |
| FR-FBC-02 | 保存时自动跑检查，错误列表来自技能输出而非前端自算 |
| FR-FBC-03 | 四个仪表数值与 `feedback-check.json` 中的字段一致 |
| FR-FBC-04 | 未勾选时导出禁用；勾选后 `monitor/review-ack.json` 含时间戳与字数 |
| FR-EXP-01 | 单题导出后页面内预览表格列数与 xlsx 一致 |
| FR-EXP-02 | 选一个含不合格题的日期，导出结果显示每题的跳过原因 |
| FR-EXP-03 | 故意删掉录屏，导出按钮禁用并列出「缺运行录屏」 |
| FR-EXP-04 | 三个按钮分别能打开 Finder / 复制路径 / 浏览器下载 |

### 6.8 观测（OPS）

| 编号 | 证明方式 |
|---|---|
| FR-OPS-01 | 看板五个分组均非空（构造数据） |
| FR-OPS-02 | 11 步进度条与命令行 `status` 输出逐项一致 |
| FR-OPS-03 | 做 3 个操作后事件流有 3 条，时间与操作时间差 < 1s |
| FR-OPS-04 | 用 `df` 模拟低磁盘（配置阈值调高到当前剩余），新建被拒 |
| FR-OPS-05 | 统计数字与 `events` 表聚合结果一致（同一 SQL） |
| FR-OPS-06 | 生成的工作报告 Markdown 含当日题数、导出数、失败原因分布 |

## 7. 非功能需求验收

| 编号 | 证明方式 |
|---|---|
| NFR-01 | Chrome DevTools Performance：看板首次渲染 < 800ms（本地） |
| NFR-02 | `kill -9` 控制台，容器仍在跑（`docker ps` 可见）；重启后 10s 内状态正确 |
| NFR-03 | 手工改 `state.json` 的 status，60s 内界面跟着变，且 `events` 有 `reconcile.corrected` |
| NFR-04 | 另开一台机器访问 `http://<本机IP>:8099` 应连不上（只绑 127.0.0.1）；日志中无明文 token |
| NFR-05 | `logs/skill/*.log` 每条含 argv、耗时、exit_code、stdout 前 4KB |
| NFR-06 | 手工把 `state.json` 的 schema 改成未知版本，界面进入只读而不是崩溃 |
| NFR-07 | `pytest -q` 全绿，且不启动任何真实容器（`docker ps` 前后一致） |
| NFR-08 | `make test-linux`（Docker 内跑）除录屏相关用例外全绿 |

## 8. 安全验收（单独列，因为它一票否决）

| # | 检查 | 方法 |
|---|---|---|
| S1 | 仓库中无凭据 | `git ls-files \| xargs grep -lE "(ghp_\|sk-\|Bearer [A-Za-z0-9]{20,})"` 无输出 |
| S2 | `.gitignore` 覆盖 `.env`、`*.db`、`drafts/`、`exports/` | 目视 |
| S3 | 日志脱敏 | 用真 token 跑一次，`grep <部分token> logs/` 应为空 |
| S4 | 页面复制命令脱敏 | 复制出的命令中 Key 为 `<from-keychain>` |
| S5 | Keychain 读取需确认 | 设置页点「显示令牌」时要求二次确认 |
| S6 | 子进程不继承无关环境变量 | 在 `run` 前打印 `env`，确认无 `AWS_*`、`SSH_*` 等 |
| S7 | 服务不监听 0.0.0.0 | `lsof -nP -iTCP:8099 -sTCP:LISTEN` 显示 127.0.0.1 |

## 9. 验收不通过的处理

| 层级失败 | 处理 |
|---|---|
| 静态检查 | 阻断提交 |
| 单元测试 | 阻断合并 |
| 端到端演练 | 阻断发版 |
| 真实试跑 | 阻断放行，且**不得以「先上线再说」跳过** |

**唯一例外**：`FR-SRC-10`（适配器可扩展）与 `FR-OPS-06`（工作报告）为 SHOULD，
可以带着已知缺陷发版，但必须在 `12-open-questions.md` 中登记。
