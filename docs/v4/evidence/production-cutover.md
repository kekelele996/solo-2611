# 生产升级与重启认领记录

- 时间：2026-11-13 19:21–19:23（Asia/Shanghai）
- 技能：`0.5.0 → 0.6.0`
- 工作台：`0.3.0 → 0.4.0`，最终 build `0.4.0+e0aa3fef9d81`

## 升级前保护

- 技能备份：`~/solo-auto/backups/solo-2611-v0.5.0-20261113-192140.tar.gz`
- 工作台数据库备份：`~/solo-auto/backups/backup-20261113-1921.zip`
- 原 v4 设计稿与输出备份：`~/solo-auto/backups/solo-auto-untracked-v4-20261113-192211.tar.gz`
- 重启前任务快照：`~/solo-auto/backups/pre-v4-restart-tasks.json`
- 重启后任务快照：`~/solo-auto/backups/post-v4-restart-tasks.json`
- 最终热修后任务快照：`~/solo-auto/backups/final-v4-running-tasks.json`

## 重启前后对比

| 项目 | 重启前 | 重启后 | 结果 |
|---|---|---|---|
| 工作台版本 | `0.3.0` | `0.4.0` | 已升级 |
| 技能版本 | `0.5.0` | `0.6.0` | 已升级 |
| running 任务 | 2 | 2 | 相同 |
| running 任务 ID | `d417…`, `1fb4…` | `d417…`, `1fb4…` | 相同 |
| 容器 | `solo2611-task-391175a0-02-a67c`、`solo2611-rpa-01-3be0` | 同名、同启动时间 | 未重启 |
| 桌面会话线程 | `01a1d3e8…`、`01a1d442…`、`01a1d3ec…` | 三个线程 ID 相同 | 已认领 |

生产启动后 5 个后台循环均为 `ok`，扩展迁移与 v4 表写入成功；旧任务保持可读，
没有因后端重启重新创建容器或重复发起会话。

随后为已验证的调度终态修复执行了第二次热重启，最终版本为 `5406f89`；running 任务 ID
仍与第一次重启后完全一致，两个容器保持同名且未重启。

## 旧会话认领缺陷与修复

第一次接入旧 print 会话时发现：旧 `state.json` 没有 `sessionRun`，却被送进新会话恢复逻辑，
导致两个仍在运行的旧任务在数据库中短暂标成 `aborted`。容器、tmux、workspace 和轨迹均未受影响。

修正内容：

1. 无 `sessionRun` 的旧状态明确路由到 `resume_print`。
2. 工作台启动时扫描 `job_status=running`、无 `sessionRun` 且容器仍存活的旧任务，并以
   `resume --task-root` 后台认领。
3. 恢复两个已知任务的数据库 running 状态和调度 active 状态后再次重启。
4. 新事件 `run.legacy_adopted` 显示两个旧容器均由新进程认领，并分别启动独立 resume 子进程。

对应技能热修标签：`v0.6.0-hotfix2`；后续 tmux 前置检查补丁为 `v0.6.0-hotfix3`，
轮次继续标记和屏幕空闲判定补丁为 `v0.6.0-hotfix4`；旧 print 接回单例锁为
`v0.6.0-hotfix5`。
实时轨迹增量读取补丁为 `v0.6.0-hotfix6`。

修复后再次重启：

- 两个旧容器都由 `run.legacy_adopted` 事件认领，并各自启动独立 `resume` 子进程。
- RPA 与数据迁移任务随后都完成并退出旧容器；任务 ID 和容器名在接回阶段未变化。
- 连续升级触发的 `crash_loop` 全局挂起由人工确认后解除，调度恢复为 `running`。
- 工作台随后正常启动新的 v4 受管任务，健康检查通过；旧 print 容器不再悬空。
- 最后一次热更新让旧会话扫描在维护循环中每 30 秒重试，最终代码为 `e0aa3fe`。
