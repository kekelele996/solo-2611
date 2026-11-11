import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  DatabaseBackup,
  Download,
  HardDrive,
  PackageOpen,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  TestTube2,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { ConfigResponse } from "../api/types";
import {
  Button,
  ErrorPanel,
  Field,
  LoadingLines,
  Panel,
  StatusDot,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";
import { APP_BUILD_ID } from "../utils/build";

type TestTarget = "skill" | "source" | "docker" | "solo2";
type TestOutcome = { ok: boolean; message: string };

export function SystemPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const logs = useQuery({ queryKey: ["logs"], queryFn: () => api.logs(200), refetchInterval: 5000 });
  const status = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, refetchInterval: 3000 });
  const [form, setForm] = useState<ConfigResponse | null>(null);
  const [password, setPassword] = useState("");
  const [solo2Password, setSolo2Password] = useState("");
  const [logQuery, setLogQuery] = useState("");
  const [logLevel, setLogLevel] = useState("");
  const [dirty, setDirty] = useState(false);
  const [tests, setTests] = useState<Partial<Record<TestTarget, TestOutcome>>>({});
  const [webhook, setWebhook] = useState("");
  const trash = useQuery({ queryKey: ["trash"], queryFn: api.trash, refetchInterval: 15_000 });

  useEffect(() => {
    // 表单有未保存的修改时，不让后台刷新把它冲掉
    if (config.data && !dirty) setForm(config.data);
  }, [config.data, dirty]);
  const update = (next: ConfigResponse) => {
    setForm(next);
    setDirty(true);
  };

  const save = useMutation({
    mutationFn: () => {
      if (!form) throw new Error("配置尚未加载");
      return api.patchConfig({
        port: form.port,
        skill_root: form.skill_root,
        tasks_dir: form.tasks_dir,
        source: {
          base_url: form.source.base_url,
          auth_mode: "cookie",
          username: form.source.username,
          timeout_seconds: form.source.timeout_seconds,
        },
        execution: form.execution,
        storage: form.storage,
        recording: form.recording,
        submission: form.submission,
        autopilot: form.autopilot,
        audit: form.audit,
        intake: form.intake,
        trash: form.trash,
        slim: form.slim,
        notify: { macos: form.notify.macos, webhook_url: webhook || undefined },
        solo2: form.solo2,
        secrets: password || solo2Password
          ? {
              ...(password ? { source_password: password } : {}),
              ...(solo2Password ? { solo2_password: solo2Password } : {}),
            }
          : undefined,
      });
    },
    onSuccess: () => {
      toast("已保存。自动提交、自动交付、抽检和计划设置将在下一次阶段或抽样时生效；通知立即生效。", "ok");
      setPassword("");
      setSolo2Password("");
      setWebhook("");
      setDirty(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const test = useMutation({
    mutationFn: (target: TestTarget) => api.testConfig(target),
    // 结果留在按钮旁边：原文不截断，也不会像 toast 一样消失
    onSuccess: (result, target) => setTests((prev) => ({ ...prev, [target]: { ok: result.ok, message: result.message } })),
    onError: (error, target) => setTests((prev) => ({ ...prev, [target]: { ok: false, message: errorText(error) } })),
  });
  const reconcile = useMutation({
    mutationFn: api.reconcile,
    onSuccess: (result) => {
      toast(result.corrected ? `对账完成，修正 ${result.corrected} 项` : "对账完成，索引与磁盘一致", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const backup = useMutation({
    mutationFn: api.backup,
    onSuccess: (result) => toast(`备份已生成：${result.path}`, "ok"),
    onError: (error) => toast(errorText(error), "error"),
  });
  const prune = useMutation({
    mutationFn: ({ dryRun, confirm = false }: { dryRun: boolean; confirm?: boolean }) => api.prune(dryRun, confirm),
    onSuccess: (result) => toast(
      `${result.dry_run ? "预演" : "已清理"} ${result.items.length} 项，共 ${(result.total_bytes / 1024 / 1024).toFixed(1)} MB`,
      "info",
    ),
    onError: (error) => toast(errorText(error), "error"),
  });
  const diagnostics = useMutation({
    mutationFn: api.diagnostics,
    onSuccess: (result) => toast(`诊断包已生成：${result.path}`, "ok"),
    onError: (error) => toast(errorText(error), "error"),
  });
  const restore = useMutation({
    mutationFn: api.restoreTrash,
    onSuccess: () => {
      toast("已从回收站恢复；任务 11 步状态仍以磁盘为准", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const deleteTrash = useMutation({
    mutationFn: api.deleteTrash,
    onSuccess: () => {
      toast("回收站条目已永久删除", "warn");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const emptyTrash = useMutation({
    mutationFn: api.emptyTrash,
    onSuccess: (result) => {
      toast(`已清空 ${result.removed.length} 个回收站条目`, "warn");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const restart = useMutation({
    mutationFn: api.restart,
    onSuccess: (result) => toast(result.hint, "info"),
    onError: (error) => toast(errorText(error), "error"),
  });

  useEffect(() => {
    const revealTrash = () => {
      if (window.location.hash === "#trash") {
        window.setTimeout(() => document.getElementById("trash")?.scrollIntoView(), 100);
      }
    };
    revealTrash();
    window.addEventListener("hashchange", revealTrash);
    return () => window.removeEventListener("hashchange", revealTrash);
  }, []);

  if (config.isPending) return <div className="page"><LoadingLines rows={8} /></div>;
  if (config.isError) return <div className="page"><ErrorPanel error={config.error} onRetry={() => void config.refetch()} /></div>;
  if (!form) return null;
  const visibleLogs = [...(logs.data?.items ?? [])]
    .reverse()
    .filter((event) => !logLevel || event.level === logLevel)
    .filter((event) => !logQuery || `${event.action} ${event.message} ${event.actor}`.toLowerCase().includes(logQuery.toLowerCase()));

  const testRow = (target: TestTarget, label: string) => {
    const outcome = tests[target];
    return (
      <div className="test-row">
        <Button
          size="small"
          disabled={dirty}
          title={dirty ? "测试用的是已保存的配置，请先保存" : undefined}
          loading={test.isPending && test.variables === target}
          onClick={() => test.mutate(target)}
        >
          <TestTube2 size={13} /> {label}
        </Button>
        {outcome ? (
          <span className={`test-result ${outcome.ok ? "is-ok" : "is-bad"}`} role="status">
            {outcome.message}
          </span>
        ) : null}
      </div>
    );
  };

  return (
    <div className="page system-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">SYSTEM / LOCAL ONLY</span>
          <h1>设置</h1>
        </div>
        <div className="page-actions">
          <Button onClick={() => backup.mutate()} loading={backup.isPending}>
            <DatabaseBackup size={14} /> 备份索引
          </Button>
          <Button onClick={() => prune.mutate({ dryRun: true })} loading={prune.isPending}>
            清理预演
          </Button>
          <Button onClick={() => diagnostics.mutate()} loading={diagnostics.isPending}>
            <Download size={14} /> 诊断包
          </Button>
          <Button onClick={() => reconcile.mutate()} loading={reconcile.isPending}>
            <RefreshCw size={14} /> 手动对账
          </Button>
          <Button
            variant="primary"
            data-primary
            data-save
            disabled={!dirty && !password && !solo2Password}
            onClick={() => save.mutate()}
            loading={save.isPending}
          >
            {dirty || password || solo2Password ? "保存配置" : "配置已保存"}
          </Button>
        </div>
      </header>

      <div className="system-grid">
        <Panel title="技能">
          <Field label="脚本目录">
            <input value={form.skill_root} onChange={(event) => update({ ...form, skill_root: event.target.value })} />
          </Field>
          <div className="config-facts">
            <StatusDot state="done" label={`版本 ${form.skill_version}`} />
            <span>Schema solo-2611/1</span>
          </div>
          {testRow("skill", "测试技能")}
        </Panel>

        <Panel title="题源">
          <Field label="Base URL">
            <input
              value={form.source.base_url}
              onChange={(event) => update({ ...form, source: { ...form.source, base_url: event.target.value } })}
            />
          </Field>
          <Field label="用户名">
            <input
              value={form.source.username}
              onChange={(event) => update({ ...form, source: { ...form.source, username: event.target.value } })}
            />
          </Field>
          <Field label="密码" hint={`当前：${form.source.password}`}>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="留空则不修改 Keychain"
            />
          </Field>
          {testRow("source", "测试连接")}
        </Panel>

        <Panel title="提交平台">
          <Field label="平台地址">
            <input
              value={form.solo2.base_url}
              onChange={(event) => update({ ...form, solo2: { ...form.solo2, base_url: event.target.value } })}
            />
          </Field>
          <Field label="平台账号">
            <input
              autoComplete="username"
              value={form.solo2.username}
              onChange={(event) => update({ ...form, solo2: { ...form.solo2, username: event.target.value } })}
            />
          </Field>
          <Field label="平台密码" hint={`当前：${form.solo2.password}`}>
            <input
              type="password"
              autoComplete="current-password"
              value={solo2Password}
              onChange={(event) => setSolo2Password(event.target.value)}
              placeholder="留空则不修改 Keychain"
            />
          </Field>
          <label className="check-inline">
            <input
              type="checkbox"
              checked={form.submission.auto_submit}
              onChange={(event) =>
                update({
                  ...form,
                  submission: { ...form.submission, auto_submit: event.target.checked },
                })
              }
            />
            自动提交到 SOLO2 <span className="effect-tag">下次导出</span>
          </label>
          <p className="form-note">
            关闭时只生成本地交付，人工检查后再执行提交。打开后会上传轨迹和录屏、提交并轮询质检；
            遇到待返修会保留平台原文并进入返修流程。
          </p>
          {testRow("solo2", "测试登录")}
        </Panel>

        <Panel title="执行">
          <Field label="容器并发">
            <input
              type="number"
              min={1}
              max={8}
              value={form.execution.max_containers}
              onChange={(event) =>
                update({
                  ...form,
                  execution: { ...form.execution, max_containers: Number(event.target.value) },
                })
              }
            />
          </Field>
          <Field label="超时秒数">
            <input
              type="number"
              value={form.execution.run_timeout_seconds}
              onChange={(event) =>
                update({
                  ...form,
                  execution: { ...form.execution, run_timeout_seconds: Number(event.target.value) },
                })
              }
            />
          </Field>
          <Field label="尝试次数">
            <input
              type="number"
              min={1}
              max={10}
              value={form.execution.attempts}
              onChange={(event) =>
                update({
                  ...form,
                  execution: { ...form.execution, attempts: Number(event.target.value) },
                })
              }
            />
          </Field>
          <Field label="任务目录">
            <input value={form.tasks_dir} onChange={(event) => update({ ...form, tasks_dir: event.target.value })} />
          </Field>
          {testRow("docker", "测试 Docker")}
        </Panel>

        <Panel title="存储与录屏">
          <Field label="磁盘阈值 GB">
            <input
              type="number"
              value={form.storage.disk_floor_gb}
              onChange={(event) =>
                update({
                  ...form,
                  storage: { ...form.storage, disk_floor_gb: Number(event.target.value) },
                })
              }
            />
          </Field>
          <Field label="录屏上限">
            <input type="number" value={form.recording.max_seconds} disabled />
          </Field>
          <Field label="Chrome URL">
            <input
              value={form.recording.chrome_url}
              onChange={(event) =>
                update({ ...form, recording: { ...form.recording, chrome_url: event.target.value } })
              }
            />
          </Field>
          <div className="inline-metrics">
            <span><HardDrive size={14} /> 剩余 {status.data?.disk_free_gb ?? "—"} GB</span>
            <span><ShieldCheck size={14} /> 仅监听 127.0.0.1</span>
            <span>GitHub Token {form.github_token}</span>
          </div>
        </Panel>

        <Panel title="自动交付">
          <label className="check-inline">
            <input
              type="checkbox"
              checked={form.autopilot.enabled}
              onChange={(event) => update({ ...form, autopilot: { ...form.autopilot, enabled: event.target.checked } })}
            />
            启用自动交付总开关 <span className="effect-tag">下次任务</span>
          </label>
          <Field label="执行方式" hint="ChatGPT 模式会在桌面端明确打开一条新会话">
            <select
              value={form.autopilot.agent_mode}
              onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, agent_mode: event.target.value as "chatgpt" | "cli" },
              })}
            >
              <option value="chatgpt">ChatGPT 桌面会话（可见）</option>
              <option value="cli">无界面 Codex CLI</option>
            </select>
          </Field>
          {form.autopilot.agent_mode === "chatgpt" ? (
            <>
              <Field label="ChatGPT 文件夹 ID" hint="可留空；填写后新会话落到指定文件夹">
                <input
                  value={form.autopilot.chatgpt_folder_id}
                  onChange={(event) => update({
                    ...form,
                    autopilot: { ...form.autopilot, chatgpt_folder_id: event.target.value },
                  })}
                  placeholder="projects.id，可留空"
                />
              </Field>
              <Field label="CodexQueuePush 路径" hint="留空自动探测；用于把长提示词准确送入 ChatGPT">
                <input
                  value={form.autopilot.chatgpt_push_helper}
                  onChange={(event) => update({
                    ...form,
                    autopilot: { ...form.autopilot, chatgpt_push_helper: event.target.value },
                  })}
                />
              </Field>
              <Field label="无进展超时（秒）">
                <input
                  type="number"
                  min={300}
                  value={form.autopilot.chatgpt_timeout_seconds}
                  onChange={(event) => update({
                    ...form,
                    autopilot: { ...form.autopilot, chatgpt_timeout_seconds: Number(event.target.value) },
                  })}
                />
              </Field>
            </>
          ) : (
            <>
              <Field label="Agent 可执行文件" hint="留空时自动探测 codex">
                <input
                  value={form.autopilot.agent_bin}
                  onChange={(event) => update({ ...form, autopilot: { ...form.autopilot, agent_bin: event.target.value } })}
                  placeholder="codex"
                />
              </Field>
              <Field label="附加参数" hint="按空格拆分">
                <input
                  value={form.autopilot.agent_args.join(" ")}
                  onChange={(event) => update({
                    ...form,
                    autopilot: { ...form.autopilot, agent_args: event.target.value.split(/\s+/).filter(Boolean) },
                  })}
                />
              </Field>
            </>
          )}
          <div className="form-grid">
            <Field label="阶段超时（秒）" hint="下次阶段">
              <input type="number" min={60} value={form.autopilot.stage_timeout_seconds} onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, stage_timeout_seconds: Number(event.target.value) },
              })} />
            </Field>
            <Field label="阶段重试">
              <input type="number" min={1} max={10} value={form.autopilot.stage_attempts} onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, stage_attempts: Number(event.target.value) },
              })} />
            </Field>
            <Field label="运行重试">
              <input type="number" min={1} max={10} value={form.autopilot.run_attempts} onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, run_attempts: Number(event.target.value) },
              })} />
            </Field>
            <Field label="起草并发">
              <input type="number" min={1} max={8} value={form.autopilot.parallel_authoring} onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, parallel_authoring: Number(event.target.value) },
              })} />
            </Field>
            <Field label="交付并发">
              <input type="number" min={1} max={8} value={form.autopilot.parallel_delivering} onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, parallel_delivering: Number(event.target.value) },
              })} />
            </Field>
            <Field label="同类失败熔断阈值">
              <input type="number" min={1} max={50} value={form.autopilot.breaker.same_kind_failures} onChange={(event) => update({
                ...form,
                autopilot: {
                  ...form.autopilot,
                  breaker: { ...form.autopilot.breaker, same_kind_failures: Number(event.target.value) },
                },
              })} />
            </Field>
          </div>
          <label className="check-inline">
            <input
              type="checkbox"
              checked={form.autopilot.stop_kills_containers}
              onChange={(event) => update({
                ...form,
                autopilot: { ...form.autopilot, stop_kills_containers: event.target.checked },
              })}
            />
            停止自动交付时默认同时中断容器 <span className="effect-tag">立即</span>
          </label>
          <label className="check-inline">
            <input
              type="checkbox"
              checked={form.autopilot.restricted}
              onChange={(event) => update({ ...form, autopilot: { ...form.autopilot, restricted: event.target.checked } })}
            />
            受限模式（workspace-write，不自动降级） <span className="effect-tag">下次阶段</span>
          </label>
        </Panel>

        <Panel title="抽检与生产计划">
          <Field label="抽检策略" hint="默认按任务类型分层抽样 30%">
            <select
              value={form.audit.mode}
              onChange={(event) => update({
                ...form,
                audit: { ...form.audit, mode: event.target.value as typeof form.audit.mode },
              })}
            >
              <option value="sample">按比例抽样</option>
              <option value="all">全检</option>
              <option value="off">不抽检</option>
            </select>
          </Field>
          <Field label="抽样比例" hint="下次抽样生效">
            <input type="number" min={0} max={1} step={0.05} value={form.audit.sample_rate} onChange={(event) => update({
              ...form,
              audit: { ...form.audit, sample_rate: Number(event.target.value) },
            })} />
          </Field>
          <Field label="在手上限" hint="0 = 起草并发 + 容器并发">
            <input type="number" min={0} max={100} value={form.intake.max_in_hand} onChange={(event) => update({
              ...form,
              intake: { ...form.intake, max_in_hand: Number(event.target.value) },
            })} />
          </Field>
          <Field label="计划排除标签" hint="逗号分隔；计划选择候选时排除">
            <input value={form.intake.exclude_tags.join(", ")} onChange={(event) => update({
              ...form,
              intake: { ...form.intake, exclude_tags: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) },
            })} />
          </Field>
          <p className="form-note">自动领取每次只领一个，并受在手上限、领取总上限、同类失败熔断和题源登录态共同约束。</p>
        </Panel>

        <Panel title="通知">
          <label className="check-inline">
            <input type="checkbox" checked={form.notify.macos} onChange={(event) => update({
              ...form,
              notify: { ...form.notify, macos: event.target.checked },
            })} />
            macOS 系统通知 <span className="effect-tag">立即</span>
          </label>
          <Field label="Webhook" hint={`当前：${form.notify.webhook_url || "未设置"}；凭据写入 Keychain`}>
            <input
              type="password"
              value={webhook}
              onChange={(event) => setWebhook(event.target.value)}
              placeholder="留空不修改；输入新地址覆盖"
            />
          </Field>
          <p className="form-note">Webhook 仅发送标题、任务名与状态，不发送题面、路径或 SessionID。</p>
        </Panel>

        <Panel title="服务与诊断">
          <div className="inline-metrics">
            <span><ShieldCheck size={14} /> 仅监听 127.0.0.1</span>
            <span className="mono">build {APP_BUILD_ID}</span>
          </div>
          <div className="form-actions">
            <Button onClick={() => { void navigator.clipboard.writeText("make install-service"); }}>复制安装命令</Button>
            <Button onClick={() => restart.mutate(false)} loading={restart.isPending}><RefreshCw size={13} /> 重启后端</Button>
            <Button onClick={() => diagnostics.mutate()} loading={diagnostics.isPending}><PackageOpen size={13} /> 导出诊断包</Button>
          </div>
          <p className="form-note mono">常驻安装：在项目根目录运行 make install-service；卸载：make uninstall-service。</p>
          {prune.data ? (
            <div className="prune-preview">
              <strong>清理预演 · {prune.data.items.length} 项 · {(prune.data.total_bytes / 1024 / 1024).toFixed(1)} MB</strong>
              <Button
                size="small"
                variant="danger"
                disabled={!prune.data.items.length}
                onClick={() => {
                  if (window.confirm("只清理暂存区和旧技能日志，不会动任务目录。确认执行？")) {
                    prune.mutate({ dryRun: false, confirm: true });
                  }
                }}
              >
                执行预演结果
              </Button>
            </div>
          ) : null}
        </Panel>

        <Panel title="回收站与瘦身" className="trash-panel">
          <div id="trash" />
          <div className="form-grid">
            <Field label="保留期（天）" hint="0 = 不自动清理">
              <input type="number" min={0} max={3650} value={form.trash.retention_days} onChange={(event) => update({
                ...form,
                trash: { ...form.trash, retention_days: Number(event.target.value) },
              })} />
            </Field>
            <Field label="导出后瘦身天数">
              <input type="number" min={0} max={365} value={form.slim.after_days} onChange={(event) => update({
                ...form,
                slim: { ...form.slim, after_days: Number(event.target.value) },
              })} />
            </Field>
          </div>
          <label className="check-inline">
            <input type="checkbox" checked={form.slim.enabled} onChange={(event) => update({
              ...form,
              slim: { ...form.slim, enabled: event.target.checked },
            })} />
            启用导出后瘦身 <span className="effect-tag">下次清理周期</span>
          </label>
          <Field label="瘦身目录名单" hint="逗号分隔；默认关闭">
            <input value={form.slim.patterns.join(", ")} onChange={(event) => update({
              ...form,
              slim: { ...form.slim, patterns: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) },
            })} />
          </Field>
          <div className="trash-summary">
            <span>{trash.data?.total ?? 0} 个条目 · {((trash.data?.total_bytes ?? 0) / 1024 / 1024).toFixed(1)} MB</span>
            <Button size="small" variant="danger" disabled={!trash.data?.items.length} loading={emptyTrash.isPending} onClick={() => {
              if (window.confirm("永久删除回收站全部条目？此操作不可恢复。")) emptyTrash.mutate();
            }}>清空回收站</Button>
          </div>
          {trash.data?.items.length ? (
            <div className="trash-list">
              {trash.data.items.map((item) => (
                <div key={item.name}>
                  <span>
                    <strong>{item.task_name}</strong>
                    <small>{formatDateTime(item.cleaned_at)} · {(item.size / 1024 / 1024).toFixed(1)} MB · {item.remaining_days === null ? "不自动清理" : `剩 ${item.remaining_days ?? 0} 天`}</small>
                  </span>
                  <div className="row-actions">
                    <Button size="small" disabled={item.legacy} title={item.legacy ? "旧条目缺少 meta.json，无法自动恢复" : undefined} onClick={() => restore.mutate(item.name)}>
                      <RotateCcw size={12} /> 恢复
                    </Button>
                    <Button size="small" variant="danger" onClick={() => deleteTrash.mutate(item.name)}><Trash2 size={12} /> 删除</Button>
                  </div>
                </div>
              ))}
            </div>
          ) : <p className="empty-inline">回收站为空。</p>}
        </Panel>
      </div>

      <Panel title="最近事件与日志" className="logs-panel">
        <div className="table-toolbar logs-toolbar">
          <label className="search-box">
            <Search size={14} />
            <input value={logQuery} onChange={(event) => setLogQuery(event.target.value)} placeholder="筛选事件" />
          </label>
          <select value={logLevel} onChange={(event) => setLogLevel(event.target.value)} aria-label="按级别筛选">
            <option value="">全部级别</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="error">error</option>
          </select>
          <span className="toolbar-count">{visibleLogs.length} 条 · 每 5 秒刷新</span>
        </div>
        {logs.isPending ? <LoadingLines rows={5} /> : null}
        {logs.isError ? <ErrorPanel error={logs.error} onRetry={() => void logs.refetch()} /> : null}
        {visibleLogs.length ? (
          <div className="event-table">
            {visibleLogs.map((event) => (
              <div key={event.id} className={`event-row event-${event.level}`}>
                <span className="mono">{formatDateTime(event.at, true)}</span>
                <strong>{event.action}</strong>
                <span title={event.message}>{event.message}</span>
                <small>{event.actor}</small>
              </div>
            ))}
          </div>
        ) : logs.isPending ? null : (
          <div className="empty-inline">{logQuery || logLevel ? "没有匹配的事件" : "暂无事件"}</div>
        )}
      </Panel>
    </div>
  );
}
