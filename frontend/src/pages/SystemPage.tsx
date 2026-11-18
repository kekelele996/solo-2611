import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  DatabaseBackup,
  Github,
  HardDrive,
  LibraryBig,
  LockKeyhole,
  PackageOpen,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  SquareArrowOutUpRight,
  TestTube2,
  Trash2,
  Waypoints,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "../api/client";
import type { AccountView, ConfigResponse } from "../api/types";
import {
  Button,
  ErrorPanel,
  Field,
  LoadingLines,
  Panel,
  StatusDot,
  TabBar,
} from "../components/ui";
import { PasswordField } from "../components/PasswordField";
import { AccountProfiles } from "../components/AccountProfiles";
import { AccountViewForm } from "../components/AccountViewPanel";
import { useToast } from "../components/toast-context";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";
import { APP_BUILD_ID } from "../utils/build";

type TestTarget = "skill" | "source" | "docker" | "solo2" | "github" | "api";
type TestOutcome = { ok: boolean; message: string };

const sections = [["accounts", "账号与连接"], ["runtime", "运行与交付"], ["backup", "备份与迁移"], ["maintenance", "维护与日志"]] as const;
type Section = typeof sections[number][0];

function sectionFromHash(): Section {
  const hash = window.location.hash.slice(1);
  if (hash === "trash") return "maintenance";
  return sections.find(([key]) => key === hash)?.[0] ?? "accounts";
}

function secretHint(marker?: string) {
  return marker === "<set>" ? "已保存，留空保留" : "尚未设置";
}

function Connection({ name, icon, saved, test, children }: {
  name: string; icon: ReactNode; saved: boolean; test: ReactNode; children: ReactNode;
}) {
  return (
    <section className="settings-connection" aria-label={name}>
      <header className="connection-heading">
        <h2>{icon}{name}</h2>
        <span className="connection-state" data-saved={saved}>{saved ? "凭据已保存" : "待设置凭据"}</span>
      </header>
      <div className="connection-fields">{children}</div>
      {test}
    </section>
  );
}

export function SystemPage() {
  const [session, setSession] = useState<{ token: string; seconds: number } | null>(null);
  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => setSession(null), session.seconds * 1000);
    return () => { window.clearTimeout(timer); void api.lockAccounts(session.token).catch(() => {}); };
  }, [session]);
  return <SystemSettings key={session?.token ?? "locked"} token={session?.token} onLock={() => setSession(null)}
    onUnlock={view => { if (view.token) setSession({ token: view.token, seconds: view.expires_in }); }} />;
}

function SystemSettings({ token, onUnlock, onLock }: { token?: string; onUnlock: (view: AccountView) => void; onLock: () => void }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [section, setSection] = useState<Section>(sectionFromHash);
  const configKey = ["config", token ?? "public"];
  const config = useQuery({ queryKey: configKey, queryFn: () => api.config(token), gcTime: 0 });
  const logs = useQuery({ queryKey: ["logs"], queryFn: () => api.logs(200), enabled: section === "maintenance", refetchInterval: 5000 });
  const status = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, enabled: section === "runtime", refetchInterval: 15000 });
  const [form, setForm] = useState<ConfigResponse | null>(null);
  const [password, setPassword] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [backupPassword, setBackupPassword] = useState("");
  const [solo2Password, setSolo2Password] = useState("");
  const [logQuery, setLogQuery] = useState("");
  const [logLevel, setLogLevel] = useState("");
  const [dirty, setDirty] = useState(false);
  const [tests, setTests] = useState<Partial<Record<TestTarget, TestOutcome>>>({});
  const [webhook, setWebhook] = useState("");
  const trash = useQuery({ queryKey: ["trash"], queryFn: api.trash, enabled: section === "maintenance", refetchInterval: 15_000 });

  const formReady = !!form;
  const formProfile = form?.active_profile;
  const hasChanges = dirty || !!(password || githubToken || solo2Password || modelKey || webhook);

  useEffect(() => {
    if (!config.data) return;
    if (formProfile && config.data.active_profile !== formProfile) {
      setPassword(""); setSolo2Password(""); setGithubToken(""); setModelKey("");
      setTests({}); setDirty(false); setForm(config.data);
    } else if (!dirty) {
      setForm(config.data);
    }
  }, [config.data, dirty, formProfile]);
  const update = (next: ConfigResponse) => {
    setForm(next);
    setDirty(true);
  };

  const save = useMutation({
    mutationFn: () => {
      if (!form) throw new Error("配置尚未加载");
      const saved = config.data;
      const changed = <K extends keyof ConfigResponse>(key: K) =>
        JSON.stringify(form[key]) !== JSON.stringify(saved?.[key]) ? form[key] : undefined;
      return api.patchConfig({
        skill_root: changed("skill_root"),
        tasks_dir: changed("tasks_dir"),
        skill: changed("model_base_url") !== undefined ? { base_url: form.model_base_url } : undefined,
        source: changed("source") ? { base_url: form.source.base_url, username: form.source.username } : undefined,
        execution: changed("execution"),
        storage: changed("storage"),
        recording: changed("recording"),
        submission: changed("submission"),
        autopilot: changed("autopilot"),
        intake: changed("intake"),
        trash: changed("trash"),
        slim: changed("slim"),
        notify: changed("notify") || webhook ? { macos: form.notify.macos, webhook_url: webhook || undefined } : undefined,
        solo2: changed("solo2") ? {username: form.solo2.username, base_url: form.solo2.base_url, locked_domain: form.solo2.locked_domain} : undefined,
        secrets: password || solo2Password || githubToken || modelKey
          ? {
              ...(modelKey ? { gateway_key: modelKey } : {}),
              ...(githubToken ? { github_token: githubToken } : {}),
              ...(password ? { source_password: password } : {}),
              ...(solo2Password ? { solo2_password: solo2Password } : {}),
            }
          : undefined,
      }, token, form.active_profile);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(configKey, saved);
      setForm(saved);
      setTests({});
      toast("配置已保存", "ok");
      setModelKey("");
      setPassword("");
      setSolo2Password("");
      setGithubToken("");
      setWebhook("");
      setDirty(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const test = useMutation({
    mutationFn: (target: TestTarget) => token ? api.testConfig(target, token) : api.testConfig(target),
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
  const migrateAccount = useMutation({
    mutationFn: () => api.migrateAccounts(token),
    onSuccess: data => { queryClient.setQueryData(configKey, data); setForm(data); setTests({}); toast("旧配置已迁入", "ok"); },
    onError: error => toast(errorText(error), "error"),
  });
  const exportAccount = useMutation({
    mutationFn: () => api.exportAccounts(backupPassword, token),
    onSuccess: bundle => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], {type: "application/json"}));
      const link = document.createElement("a");
      link.href = url;
      link.download = "solo-账号配置.json";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    onError: error => toast(errorText(error), "error"),
  });
  const importAccount = useMutation({
    mutationFn: async (file: File) => api.importAccounts(JSON.parse(await file.text()), backupPassword, token),
    onSuccess: data => { queryClient.setQueryData(configKey, data); setForm(data); setTests({}); toast("账号配置已导入", "ok"); },
    onError: error => toast(errorText(error), "error"),
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
    const sync = () => setSection(sectionFromHash());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => {
    if (section === "maintenance" && window.location.hash === "#trash") {
      document.getElementById("trash")?.scrollIntoView?.();
    }
  }, [section, formReady]);

  if (config.isPending) return <div className="page"><LoadingLines rows={8} /></div>;
  if (config.isError && !config.data) return <div className="page"><ErrorPanel error={config.error} onRetry={() => void config.refetch()} /></div>;
  if (!form) return null;
  const accountsLocked = config.data?.accounts_locked || form.accounts_locked;
  const visibleLogs = [...(logs.data?.items ?? [])]
    .reverse()
    .filter((event) => !logLevel || event.level === logLevel)
    .filter((event) => !logQuery || `${event.action} ${event.message} ${event.actor}`.toLowerCase().includes(logQuery.toLowerCase()));

  const testRow = (target: TestTarget, label: string) => {
    const outcome = tests[target];
    const unsaved = hasChanges;
    return (
      <div className="test-row">
        <Button
          size="small"
          disabled={unsaved || test.isPending}
          title={unsaved ? "测试用的是已保存的配置，请先保存" : undefined}
          loading={test.isPending && test.variables === target}
          onClick={() => test.mutate(target)}
        >
          <TestTube2 size={13} /> {label}
        </Button>
        {outcome && !unsaved ? (
          <span className={`test-result ${outcome.ok ? "is-ok" : "is-bad"}`} role="status">
            {outcome.message}
          </span>
        ) : null}
      </div>
    );
  };

  const accountConnections = !accountsLocked && (
    <div className="settings-connections">
        <Connection name="SOLO2 提交平台" icon={<SquareArrowOutUpRight size={17} aria-hidden="true" />}
          saved={form.solo2.password === "<set>"} test={testRow("solo2", "测试登录")}>
          <Field label="平台地址">
            <input
              value={form.solo2.base_url}
              onChange={(event) => update({ ...form, solo2: { ...form.solo2, locked_domain: "", base_url: event.target.value } })}
            />
          </Field>
          <Field label="平台账号">
            <input
              autoComplete="username"
              value={form.solo2.username}
              onChange={(event) => update({ ...form, solo2: { ...form.solo2, locked_domain: "", username: event.target.value } })}
            />
          </Field>
          <PasswordField label="平台密码" hint={secretHint(form.solo2.password)} placeholder="输入新密码"
            value={solo2Password} onChange={setSolo2Password} saved={token && form.active_profile && form.solo2.password === "<set>"
              && form.solo2.username === config.data?.solo2.username && form.solo2.base_url === config.data?.solo2.base_url
              ? { token, profile: form.active_profile, target: "solo2" } : undefined} />
          <Field label="账号绑定领域" hint="技能使用此领域出题和提交">
            <select value={form.solo2.locked_domain || ""} onChange={e => update({...form, solo2: {...form.solo2, locked_domain: e.target.value as typeof form.solo2.locked_domain}})}>
              {["", "安全", "游戏、图形与多媒体", "数据工程"].map(value => <option key={value} value={value}>{value || "未绑定"}</option>)}
            </select>
          </Field>
        </Connection>

        <Connection name="题源账号" icon={<LibraryBig size={17} aria-hidden="true" />}
          saved={form.source.password === "<set>"} test={testRow("source", "测试连接")}>
          <div className="connection-address">
            <Field label="题源地址">
              <input
                value={form.source.base_url}
                onChange={(event) => update({ ...form, source: { ...form.source, base_url: event.target.value } })}
              />
            </Field>
          </div>
          <Field label="用户名">
            <input
              value={form.source.username}
              onChange={(event) => update({ ...form, source: { ...form.source, username: event.target.value } })}
            />
          </Field>
          <PasswordField label="密码" hint={secretHint(form.source.password)} placeholder="输入新密码"
            value={password} onChange={setPassword} saved={token && form.active_profile && form.source.password === "<set>"
              && form.source.username === config.data?.source.username && form.source.base_url === config.data?.source.base_url
              ? { token, profile: form.active_profile, target: "source" } : undefined} />
        </Connection>

        <Connection name="GitHub 快照仓库" icon={<Github size={17} aria-hidden="true" />}
          saved={form.github_token === "<set>"} test={testRow("github", "测试 GitHub 连接")}>
          <PasswordField label="GitHub 凭据" hint={secretHint(form.github_token)} placeholder="输入新凭据"
            value={githubToken} onChange={setGithubToken}
            saved={token && form.active_profile && form.github_token === "<set>"
              ? { token, profile: form.active_profile, target: "github" } : undefined} />
        </Connection>

        <Connection name="模型网关" icon={<Waypoints size={17} aria-hidden="true" />}
          saved={form.gateway_key === "<set>"} test={testRow("api", "探测 API")}>
          <Field label="网关地址"><input value={form.model_base_url} onChange={e => update({...form, model_base_url: e.target.value})} /></Field>
          <PasswordField label="团队密钥" hint={secretHint(form.gateway_key)} placeholder="输入新密钥"
            value={modelKey} onChange={setModelKey} saved={token && form.active_profile && form.gateway_key === "<set>"
              && form.model_base_url === config.data?.model_base_url
              ? { token, profile: form.active_profile, target: "gateway" } : undefined} />
        </Connection>
    </div>
  );

  return (
    <div className="page system-page">
      <header className="page-head">
        <div>
          <h1>设置</h1>
        </div>
        {(section !== "accounts" || !accountsLocked || hasChanges) && <div className="page-actions">
          <Button
            variant="primary"
            data-primary
            data-save
            disabled={!hasChanges || test.isPending}
            onClick={() => save.mutate()}
            loading={save.isPending}
          >
            {hasChanges ? "保存配置" : "配置已保存"}
          </Button>
        </div>}
      </header>

      <TabBar shortcuts={false} label="设置分类" tabs={sections} value={section} onChange={next => {
        setSection(next);
        window.history.replaceState(window.history.state, "", `#${next}`);
      }} />
      <fieldset className="settings-content" disabled={save.isPending || migrateAccount.isPending || importAccount.isPending}>
      {section === "accounts" && <div className="account-settings" role="tabpanel" aria-label="账号与连接">
        {(!token || accountsLocked) && <section className="accounts-access" aria-labelledby="account-code-heading">
          <div className="accounts-access-intro">
            <LockKeyhole size={20} aria-hidden="true" />
            <h2 id="account-code-heading">账号查看码</h2>
            <p>解锁后可切换用户配置，编辑 SOLO2、题源、GitHub 和模型网关的连接信息。</p>
          </div>
          <AccountViewForm onUnlock={onUnlock} />
        </section>}
        {token && !accountsLocked ? <AccountProfiles token={token} canCreate
          disabled={hasChanges || test.isPending}
          switchHint={hasChanges ? "请先保存修改，再切换用户配置" : undefined}
          onLock={onLock}
          details={(_, profile) => form.active_profile === profile ? accountConnections : config.isRefetchError
            ? <ErrorPanel error={config.error} onRetry={() => void config.refetch()} />
            : <LoadingLines rows={5} />}
        /> : accountConnections}
      </div>}
      {section === "backup" && accountsLocked && <Panel title="账号查看码"><AccountViewForm onUnlock={onUnlock} /></Panel>}
      {section === "runtime" && <div className="system-grid" role="tabpanel" aria-label="运行与交付">
        <Panel title="执行">
          <div className="form-note">
            容器并发、会话并发、在手上限、启动间隔与失败策略已移到 <a className="text-link" href="/dispatch">调度页</a>。
          </div>
          <Field label="运行模式">
            <select
              value={form.execution.run_mode}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    run_mode: event.target.value as "supervised" | "print",
                  },
                })
              }
            >
              <option value="supervised">受管交互（推荐）</option>
              <option value="print">无头回退</option>
            </select>
          </Field>
          <label className="check-inline">
            <input
              type="checkbox"
              checked={form.execution.open_terminal_on_run}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    open_terminal_on_run: event.target.checked,
                  },
                })
              }
            />
            启动时打开终端窗口 <span className="effect-tag">新会话 / 仅 macOS</span>
          </label>
          <p className="form-note">用于直接观察模型运行；关闭窗口只退出查看，不会停止容器和任务。</p>
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
          <Field label="继续上限" hint="最多 2 次，0 表示关闭继续">
            <input
              type="number"
              min={0}
              max={2}
              value={form.execution.continue_max}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    continue_max: Math.max(0, Math.min(2, Number(event.target.value))),
                  },
                })
              }
            />
          </Field>
          <Field label="等待处理时限（分钟）">
            <input
              type="number"
              min={1}
              max={1440}
              value={form.execution.fatal_hold_minutes}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    fatal_hold_minutes: Number(event.target.value),
                  },
                })
              }
            />
          </Field>
          <Field label="无进展时限（秒）">
            <input
              type="number"
              min={60}
              value={form.execution.no_progress_seconds}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    no_progress_seconds: Number(event.target.value),
                  },
                })
              }
            />
          </Field>
          <Field label="静默超时（秒）">
            <input
              type="number"
              min={60}
              value={form.execution.silent_seconds}
              onChange={(event) =>
                update({
                  ...form,
                  execution: {
                    ...form.execution,
                    silent_seconds: Number(event.target.value),
                  },
                })
              }
            />
          </Field>
          <Field label="任务目录">
            <input value={form.tasks_dir} onChange={(event) => update({ ...form, tasks_dir: event.target.value })} />
          </Field>
          {testRow("docker", "测试 Docker")}
        </Panel>

        <Panel title="自动交付">
          <Field label="每个账号的每日提交上限" hint="北京时间 00:00 换日，只统计首次提交。">
            <input type="number" min={1} max={10000} value={form.submission.daily_new_limit ?? 70}
              onChange={(event) => update({ ...form, submission: { ...form.submission, daily_new_limit: Number(event.target.value) } })} />
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
            自动提交到 SOLO2 <span className="effect-tag">默认开启</span>
          </label>
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
              <Field label="有效进展超时（秒）" hint="仅源码、门禁、轨迹和交付产物变化算进展">
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
            <Field label="任务总超时（秒）" hint="从任务启动累计；超时后强制释放容器">
              <input
                type="number"
                min={300}
                value={form.autopilot.task_timeout_seconds}
                onChange={(event) => update({
                  ...form,
                  autopilot: { ...form.autopilot, task_timeout_seconds: Number(event.target.value) },
                })}
              />
            </Field>
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

        <Panel title="技能">
          <Field label="脚本目录">
            <input value={form.skill_root} onChange={(event) => update({ ...form, skill_root: event.target.value })} />
          </Field>
          <div className="config-facts">
            <StatusDot state="done" label={`版本 ${form.skill_version}`} />
          </div>
          {testRow("skill", "测试技能")}
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
          </div>
        </Panel>

        <Panel title="生产计划">
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
      </div>}
      {section === "backup" && !accountsLocked && <div className="system-grid" role="tabpanel" aria-label="备份与迁移">
        <Panel title="账号配置备份">
          <Field label="账号备份密码" hint="备份 SOLO2 账号、领域和平台地址。填写密码时一并加密保存 SOLO2 密码和 GitHub 凭据。"><input type="password" autoComplete="new-password" value={backupPassword} onChange={e => setBackupPassword(e.target.value)} /></Field>
          <Button onClick={() => exportAccount.mutate()} disabled={hasChanges} loading={exportAccount.isPending}>导出账号配置</Button>
          <Field label="导入账号配置"><input type="file" accept="application/json,.json" disabled={hasChanges || importAccount.isPending} onChange={event => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) importAccount.mutate(file);
          }} /></Field>
        </Panel>

        <Panel title="旧设备配置迁移">
          <p className="form-note">在本机首次升级时使用。迁入旧技能的领域、GitHub 凭据和模型配置，已设置的账号凭据会保留。</p>
          <Button onClick={() => migrateAccount.mutate()} disabled={hasChanges} loading={migrateAccount.isPending}>迁入旧技能配置</Button>
        </Panel>
      </div>}
      {section === "maintenance" && <div className="system-grid" role="tabpanel" aria-label="维护与日志">
        <Panel title="服务与诊断">
          <div className="inline-metrics">
            <span><ShieldCheck size={14} /> 仅监听 127.0.0.1</span>
            <span className="mono">build {APP_BUILD_ID}</span>
          </div>
          <div className="form-actions">
          <Button onClick={() => backup.mutate()} loading={backup.isPending}>
            <DatabaseBackup size={14} /> 备份索引
          </Button>
          <Button onClick={() => prune.mutate({ dryRun: true })} loading={prune.isPending}>
            清理预演
          </Button>
          <Button onClick={() => reconcile.mutate()} loading={reconcile.isPending}>
            <RefreshCw size={14} /> 手动对账
          </Button>
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
      </div>}
      </fieldset>
      {section === "maintenance" && <Panel title="最近事件与日志" className="logs-panel">
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
      </Panel>}
    </div>
  );
}
