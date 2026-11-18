# 项目记忆与历史提示词

更新时间：2026-11-16

## 目标

把已经确认过的工程结构、运行命令、失败模式和提示词历史保存到宿主机，供下一次项目会话直接读取，减少重复摸仓库和重复出题。

项目记忆只解决“复用工程理解”，不改变一题一容器、一 Session、一条 pointdata 的固定口径。

## 存储位置

默认目录：

```text
$SOLO_AUTO_HOME/project-memory/<project-id>/
  project.md
  engineering/<engineering-id>/
    analysis.md
    context.json
    repo-map.md
    test-playbook.md
    risks.json
    prompts.jsonl
    index.json
```

- `analysis.md`：供编排模型快速阅读的工程摘要。
- `context.json`：任务状态、快照、Feedback、QC 和失败模式的结构化事实。
- `repo-map.md`：工作区目录、代码分布、重点文件和 README 摘要。
- `test-playbook.md`：真实执行过的命令、退出码、耗时和日志位置。
- `risks.json`：失败类型、最近失败和低分任务。
- `prompts.jsonl`：历史题面、哈希、结果和质量状态。

所有文件只存宿主机，目录权限为 `0700`，文件权限为 `0600`。

## 安全边界

项目记忆是编排模型的私有输入，不能进入 Claude Code 执行上下文：

- 不写入任务的 `workspace/`。
- 不写入 `delivery/提示词.md`。
- 不挂载到 Claude Code 容器。
- 不进入 Claude Code 的 `$TASK_ROOT/monitor/runs/` 或轨迹目录。
- 新任务只在 `monitor/context-pack.md` 和 `monitor/context-sources.json` 中冻结本次读取的上下文来源。

Claude Code 容器只挂载：

1. `TASK_ROOT/workspace` → `/workspace`
2. `TASK_ROOT/monitor/runs/attempt-NN/live` → `/home/node/.claude/projects`
3. managed settings → `/etc/claude-code`（只读）

因此项目记忆和 `monitor/context-pack.md` 不会进入执行模型可见路径。

## 会话流程

1. 启动或领取任务时，平台按任务的项目 ID 和工程 ID 读取项目记忆。
2. 平台生成 `monitor/context-pack.md`，并由编排提示词要求先读取该文件。
3. 编排模型复用工程结构、测试命令和风险信息，但新题必须与 `prompts.jsonl` 中的历史题面实质不同。
4. 任务完成、失败或进入终态时，平台同步追加本次提示词和结果。
5. 每次工作台启动时回填最近 24 小时内创建或导出的任务。

## 历史回填

回填口径是 `created_at` 或 `exported_at` 落在最近 24 小时。旧的 `updated_at` 不用于回填，避免重启刷新索引时把很久以前的任务混进来。

任务主表缺少 `project_id` 或 `engineering_id` 时，使用 `usage_ledger` 中同一 `task_id` 的领取记录补全。

回填标记写入：

```text
$SOLO_AUTO_HOME/project-memory/.backfill.json
```

## 提示词复用规则

- 可直接读取历史题面用于查重、风格参考和排除重复方向。
- 不得直接复制历史题面作为新题。
- 相似度只是预警；必须同时检查业务问题、冲突点、状态迁移、关键取舍和业务后果。
- 只改业务名词、字段名、页面名称或顺序的变体应拒绝。
