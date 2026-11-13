# 监督器故障接回实测

- 时间：2026-11-13 19:10–19:13（Asia/Shanghai）
- 隔离任务：`/tmp/solo2611-accept/2026-11-13-v4-supervisor-resume-probe-6145`
- 原监督器 PID：`30323`
- 接回后 PID：`30463`
- SessionID：`e5471ef2-c330-4865-8d3f-fa79a14c2a9f`
- PromptID：`723fef8b-8b72-4a7b-bef7-7429162ca735`

## 实际结果

1. 题面发送且容器运行后，对监督器执行 `kill -9`。
2. `run-status` 立即显示 `supervisorAlive=false`，但 `containerRunning=true`、`tmuxAlive=true`。
3. `resume` 启动新监督器，消息为“监督器已接回”。
4. 接回后的监督器继续读取同一轨迹，模型完成 5 次工具调用后正常 `end_turn`。
5. 最终会话被接受；继续次数仍为 `0/2`，没有新建容器、没有重新发送题面。

## 证据

- 最终状态：[supervisor-resume-state.json](./supervisor-resume-state.json)
- 监督器事件：[supervisor-resume-events.jsonl](./supervisor-resume-events.jsonl)
