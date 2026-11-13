# 受管会话真实冒烟记录

- 时间：2026-11-13 19:05–19:06（Asia/Shanghai）
- 隔离任务：`/tmp/solo2611-accept/2026-11-13-v4-interactive-probe-ujhv`
- 镜像：`adminfather/benzhi-claude-code2:20261110`
- 容器：`solo2611-v4-interactive-probe-01-4ccb`
- Harness：Claude Code `2.1.319`，入口 `cli`
- SessionID：`d9d5073d-38bf-4dc3-8733-ddce9a3c3b17`
- PromptID：`ee71f932-3d13-4d65-a9aa-3a40cc1e1278`

## 实际结果

1. `run` 在 0.3 秒内返回受管启动 JSON，随后监督器独立运行。
2. 题面经 tmux 原文送入；实时 bind 在 2 秒内产生 JSONL。
3. 模型返回 `end_turn`，监督器判定第 1 轮完整，未使用继续次数。
4. 收尾时两次 `C-d` 未退出 REPL；关闭 tmux 后容器正常变为 `exited`，轨迹完整。
5. `docker cp`、轨迹校验、产物快照和接受动作完成，`run-status` 最终为 `accepted`。
6. 验收后用 `docker ps -a --filter name=solo2611-v4-interactive-probe` 复核，无残留容器。

## 证据

- 探针结果：[probe-interactive.json](./probe-interactive.json)
- 最终状态：[managed-session-state.json](./managed-session-state.json)
- 监督器事件：[managed-session-supervisor.jsonl](./managed-session-supervisor.jsonl)
- 基线：[baseline.json](./baseline.json)
- 工作台自动化：[ui-check.json](./ui-check.json)
- 会话面板：[ui-task-session.png](./ui-task-session.png)
- 调度容量：[ui-dispatch-session.png](./ui-dispatch-session.png)
- 设置页：[ui-system-session-settings.png](./ui-system-session-settings.png)
