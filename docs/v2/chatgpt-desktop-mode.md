# ChatGPT 桌面执行模式

更新时间：2026-11-11

## 目标

自动交付默认不再启动不可见的 `codex exec`。生产线必须像 `sologsb-0917` 的监控台一样，
在 ChatGPT 桌面端明确创建一条可见会话，让操作者能直接看到 Agent 的完整执行过程。

## 启动链路

1. 平台为任务生成完整、可审计的本地指令文件：
   `~/solo-auto/desktop/<taskId>/chatgpt-<time>.md`。
2. 平台构造短提示词，明确要求 ChatGPT 使用 `$solo-2611` 并读取上述指令文件。
3. `CodexQueuePush` 打开：
   `codex://threads/new?path=~/solo-auto/chatgpt-workspaces/YYYY-MM-DD&mode=work&prompt=<shortPrompt>`。
   ChatGPT 文件夹按自然日复用；任务根目录只写在短提示词和完整指令文件中。
4. ChatGPT 桌面端创建新 thread 并提交首条消息；提示词不再通过 URL 传递数 KB 正文。
5. 平台在 `~/.codex/sessions/` 中查找同时满足以下条件的 rollout：
   - 首条用户消息包含本次唯一 marker；
   - `session_meta.cwd` 的规范化路径等于任务目录。
6. 生产线实时读取该 rollout，并继续用磁盘 `state.json`、证据和技能门禁决定任务结局。

## 状态边界

- ChatGPT 会话负责执行前 10 项：出题、真实容器单轮、轨迹、录屏、Feedback 检查。
- 平台负责最终确认、单题 xlsx、抽检与提交批次。
- 平台不根据 ChatGPT 的自然语言“完成”直接放行；必须以磁盘和技能门禁为准。
- ChatGPT 一个桌面会话覆盖从出题到交付的全流程，因此共享并发上限为
  `parallel_authoring + parallel_delivering`，默认 `2 + 1 = 3`。超过上限的任务停在
  `pending`，会话结束后按顺序补位。
- 从生产线点击“打开 ChatGPT 会话”只打开已有 thread，不会再次提交提示词。
- 平台停止只能停止跟踪并清理容器；已经打开的 ChatGPT 会话可能需要在该会话内手动停止。

## 配置

`config.toml` 的 `[autopilot]`：

```toml
agent_mode = "chatgpt"       # chatgpt / cli
chatgpt_folder_id = ""       # 可留空，默认按任务目录建立 ChatGPT 文件夹
chatgpt_push_helper = ""     # 留空自动探测 CodexQueuePush
chatgpt_timeout_seconds = 43200
```

只有需要在当前运行中的 Agent 停止后，才允许切换 `agent_mode`，避免同一任务在 CLI 和
ChatGPT 中同时执行。
