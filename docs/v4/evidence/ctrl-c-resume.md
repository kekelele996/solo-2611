# Ctrl-C 脱离与接回实测

- 时间：2026-11-13 19:26–19:29（Asia/Shanghai）
- 隔离任务：`/tmp/solo2611-accept/2026-11-13-v4-ctrl-c-detach-probe-2-mqp3`
- 容器：`solo2611-v4-ctrl-c-detach-probe-2-01-e796`
- SessionID：`7912384e-2eb1-4c34-9912-a075e778128b`
- PromptID：`2253c532-6547-44bb-ac2f-d17eb2f4e928`

## 实际结果

1. `run --mode print` 运行中对 Python 进程发送 Ctrl-C。
2. 技能只脱离调用方；容器仍为 `running`，尝试仍为 `running`，workspace HEAD 仍等于初始快照。
3. 随后执行 `resume`，它等待原容器结束、导出轨迹并完成结构校验。
4. 最终状态为 `accepted`，没有停容器、重置 workspace 或重新创建会话。

另发现并修复一个真实边界：模型后台任务完成时会产生
`promptSource=system`、`turnOrigin=task_notification` 的 user 记录。轨迹校验现在明确忽略这类系统通知，
不再误判成“非继续的真人输入”。

证据：[ctrl-c-resume-state.json](./ctrl-c-resume-state.json)
