# print 回退实测

- 时间：2026-11-13 19:24（Asia/Shanghai）
- 隔离任务：`/tmp/solo2611-accept/2026-11-13-v4-print-fallback-probe-rpjl`
- 命令：`run --mode print --timeout 300`
- 结果：11.35 秒内正常结束并接受
- SessionID：`537c571a-1ce1-46f6-a9ee-105823feb4c6`
- PromptID：`956acd05-c545-4897-9d9a-736001e7efdb`

会话记录明确为 `mode=print`、继续预算 `0/0`；运行后容器已删除，生产配置仍保持
`claude.runMode=supervised`。

证据：[print-fallback-state.json](./print-fallback-state.json)
