import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, HardDrive, RefreshCw, ShieldCheck, TestTube2 } from "lucide-react";
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
import { formatDateTime } from "../utils/time";

export function SystemPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const logs = useQuery({ queryKey: ["logs"], queryFn: () => api.logs(200), refetchInterval: 5000 });
  const status = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, refetchInterval: 3000 });
  const [form, setForm] = useState<ConfigResponse | null>(null);
  const [password, setPassword] = useState("");
  const [logQuery, setLogQuery] = useState("");
  const [logLevel, setLogLevel] = useState("");

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

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
        secrets: password ? { source_password: password } : undefined,
      });
    },
    onSuccess: () => {
      toast("配置已保存", "ok");
      setPassword("");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const test = useMutation({
    mutationFn: (target: "skill" | "source" | "docker") => api.testConfig(target),
    onSuccess: (result) => toast(result.message, result.ok ? "ok" : "error"),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const reconcile = useMutation({
    mutationFn: api.reconcile,
    onSuccess: (result) => {
      toast(`对账完成，修正 ${result.corrected} 项`, "ok");
      void queryClient.invalidateQueries();
    },
  });
  const backup = useMutation({
    mutationFn: api.backup,
    onSuccess: (result) => toast(`备份已生成：${result.path}`, "ok"),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const prune = useMutation({
    mutationFn: (dryRun: boolean) => api.prune(dryRun),
    onSuccess: (result) => toast(
      `${result.dry_run ? "预演" : "已清理"} ${result.items.length} 项，共 ${(result.total_bytes / 1024 / 1024).toFixed(1)} MB`,
      "info",
    ),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  if (config.isPending) return <div className="page"><LoadingLines rows={8} /></div>;
  if (config.isError) return <div className="page"><ErrorPanel error={config.error} onRetry={() => void config.refetch()} /></div>;
  if (!form) return null;
  const visibleLogs = [...(logs.data?.items ?? [])]
    .reverse()
    .filter((event) => !logLevel || event.level === logLevel)
    .filter((event) => !logQuery || `${event.action} ${event.message} ${event.actor}`.toLowerCase().includes(logQuery.toLowerCase()));

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
          <Button onClick={() => prune.mutate(true)} loading={prune.isPending}>
            清理预演
          </Button>
          <Button onClick={() => reconcile.mutate()} loading={reconcile.isPending}>
            <RefreshCw size={14} /> 手动对账
          </Button>
          <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending}>
            保存配置
          </Button>
        </div>
      </header>

      <div className="system-grid">
        <Panel title="技能">
          <Field label="脚本目录">
            <input value={form.skill_root} onChange={(event) => setForm({ ...form, skill_root: event.target.value })} />
          </Field>
          <div className="config-facts">
            <StatusDot state="done" label={`版本 ${form.skill_version}`} />
            <span>Schema solo-2611/1</span>
          </div>
          <Button size="small" onClick={() => test.mutate("skill")}><TestTube2 size={13} /> 测试技能</Button>
        </Panel>

        <Panel title="题源">
          <Field label="Base URL">
            <input
              value={form.source.base_url}
              onChange={(event) => setForm({ ...form, source: { ...form.source, base_url: event.target.value } })}
            />
          </Field>
          <Field label="用户名">
            <input
              value={form.source.username}
              onChange={(event) => setForm({ ...form, source: { ...form.source, username: event.target.value } })}
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
          <Button size="small" onClick={() => test.mutate("source")}><TestTube2 size={13} /> 测试连接</Button>
        </Panel>

        <Panel title="执行">
          <Field label="容器并发">
            <input
              type="number"
              min={1}
              max={8}
              value={form.execution.max_containers}
              onChange={(event) =>
                setForm({
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
                setForm({
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
                setForm({
                  ...form,
                  execution: { ...form.execution, attempts: Number(event.target.value) },
                })
              }
            />
          </Field>
          <Field label="任务目录">
            <input value={form.tasks_dir} onChange={(event) => setForm({ ...form, tasks_dir: event.target.value })} />
          </Field>
          <Button size="small" onClick={() => test.mutate("docker")}><TestTube2 size={13} /> 测试 Docker</Button>
        </Panel>

        <Panel title="存储与录屏">
          <Field label="磁盘阈值 GB">
            <input
              type="number"
              value={form.storage.disk_floor_gb}
              onChange={(event) =>
                setForm({
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
                setForm({ ...form, recording: { ...form.recording, chrome_url: event.target.value } })
              }
            />
          </Field>
          <div className="inline-metrics">
            <span><HardDrive size={14} /> 剩余 {status.data?.disk_free_gb ?? "—"} GB</span>
            <span><ShieldCheck size={14} /> 仅监听 127.0.0.1</span>
            <span>GitHub Token {form.github_token}</span>
          </div>
        </Panel>
      </div>

      <Panel title="最近事件与日志" className="logs-panel">
        <div className="table-toolbar logs-toolbar">
          <label className="search-box">
            <HardDrive size={14} />
            <input value={logQuery} onChange={(event) => setLogQuery(event.target.value)} placeholder="筛选事件" />
          </label>
          <select value={logLevel} onChange={(event) => setLogLevel(event.target.value)}>
            <option value="">全部级别</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="error">error</option>
          </select>
        </div>
        {logs.isPending ? <LoadingLines rows={5} /> : null}
        {logs.isError ? <ErrorPanel error={logs.error} /> : null}
        {visibleLogs.length ? (
          <div className="event-table">
            {visibleLogs.map((event) => (
              <div key={event.id} className={`event-row event-${event.level}`}>
                <span className="mono">{formatDateTime(event.at, true)}</span>
                <strong>{event.action}</strong>
                <span>{event.message}</span>
                <small>{event.actor}</small>
              </div>
            ))}
          </div>
        ) : <div className="empty-inline"><DatabaseBackup size={16} /> 暂无事件</div>}
      </Panel>
    </div>
  );
}
