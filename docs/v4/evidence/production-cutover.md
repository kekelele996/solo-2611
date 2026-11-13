# 生产升级与重启认领记录

- 时间：2026-11-13 19:21–19:23（Asia/Shanghai）
- 技能：`0.5.0 → 0.6.0`
- 工作台：`0.3.0 → 0.4.0`，build `0.4.0+5ea69dc55e62`

## 升级前保护

- 技能备份：`~/solo-auto/backups/solo-2611-v0.5.0-20261113-192140.tar.gz`
- 工作台数据库备份：`~/solo-auto/backups/backup-20261113-1921.zip`
- 原 v4 设计稿与输出备份：`~/solo-auto/backups/solo-auto-untracked-v4-20261113-192211.tar.gz`
- 重启前任务快照：`~/solo-auto/backups/pre-v4-restart-tasks.json`
- 重启后任务快照：`~/solo-auto/backups/post-v4-restart-tasks.json`

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
