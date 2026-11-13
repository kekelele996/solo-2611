# 自动“继续”真实验证

- 时间：2026-11-13 19:47–19:54（Asia/Shanghai）
- 隔离任务：`/tmp/solo2611-accept/2026-11-13-v4-auto-continue-probe-a18f`
- SessionID：`c784b203-c2a4-46c3-a016-66d9b2fdc511`
- 最终状态：`accepted`
- 继续次数：`1/2`
- 最终备注：`模型运行中途中断，人工输入"继续"1次后完成`

## 实际链路

1. 首轮模型只输出“接下来我会继续。”，`end_turn` 后命中 `PLAN_PROMISE`，分类为 `incomplete`。
2. 监督器等待 5 秒后自动发送字面“继续”，计为第 1 次。
3. 第二轮模型继续读取并核对工作区后给出最终回复，轨迹结构校验通过。
4. 收尾时两次 Ctrl-D 未退出，关闭 tmux 后容器正常结束，轨迹完整。
5. `run-status` 显示 2 轮、继续 1/2、最终 `accepted`，继续说明已写入 outcome remark。

事件日志：[auto-continue-events.jsonl](./auto-continue-events.jsonl)

本次还修复了两个真实边界：

- `working tree clean` 曾被屏幕忙碌规则误判为模型仍在运行，导致第二轮不判定结束；现在只识别
  `esc to interrupt` / `ctrl+c to interrupt`。
- 继续发送后，持久化轮次里的 `continued` 标记没有随重新加载后的 state 更新；后续状态机在发送前
  直接标记当前最后一轮，保证轮次明细与事件日志一致。
