import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, HardDrive, RefreshCw, Search, ShieldCheck, TestTube2 } from "lucide-react";
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

type TestTarget = "skill" | "source" | "docker";
type TestOutcome = { ok: boolean; message: string };

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
  const [dirty, setDirty] = useState(false);
  const [tests, setTests] = useState<Partial<Record<TestTarget, TestOutcome>>>({});

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
        secrets: password ? { source_password: password } : undefined,
      });
    },
    onSuccess: () => {
      toast("配置已保存", "ok");
      setPassword("");
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
    mutationFn: (dryRun: boolean) => api.prune(dryRun),
    onSuccess: (result) => toast(
      `${result.dry_run ? "预演" : "已清理"} ${result.items.length} 项，共 ${(result.total_bytes / 1024 / 1024).toFixed(1)} MB`,
      "info",
    ),
    onError: (error) => toast(errorText(error), "error"),
  });

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
          <Button onClick={() => prune.mutate(true)} loading={prune.isPending}>
            清理预演
          </Button>
          <Button onClick={() => reconcile.mutate()} loading={reconcile.isPending}>
            <RefreshCw size={14} /> 手动对账
          </Button>
          <Button
            variant="primary"
            data-primary
            data-save
            disabled={!dirty && !password}
            onClick={() => save.mutate()}
            loading={save.isPending}
          >
            {dirty || password ? "保存配置" : "配置已保存"}
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
