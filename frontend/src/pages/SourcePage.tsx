import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FolderInput, LogIn, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { EngineeringRow, ProjectRow, StagingRow } from "../api/types";
import {
  Badge,
  Button,
  EmptyState,
  ErrorPanel,
  Field,
  LoadingLines,
  Modal,
  Panel,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { PromptLibraryPanel } from "../components/source/PromptLibraryPanel";
import { errorText, formatNames } from "../utils/display";
import { formatDateTime } from "../utils/time";

export function SourcePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects() });
  const system = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus, refetchInterval: 3000 });
  const [projectId, setProjectId] = useState(() => localStorage.getItem("solo-auto-project") ?? "");
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [minFiles, setMinFiles] = useState("");
  const [maxFiles, setMaxFiles] = useState("");
  const [claimTarget, setClaimTarget] = useState<EngineeringRow | null>(null);
  const [stagingTarget, setStagingTarget] = useState<StagingRow | null>(null);
  const [taskName, setTaskName] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [unusedOnly, setUnusedOnly] = useState(false);
  const [sortBy, setSortBy] = useState<"used" | "delivered" | "submitted" | "">("");

  useEffect(() => {
    if (projectId) localStorage.setItem("solo-auto-project", projectId);
  }, [projectId]);
  useEffect(() => {
    if (!projectId && projects.data?.items.length) setProjectId(projects.data.items[0].id);
  }, [projectId, projects.data?.items]);

  const engineering = useQuery({
    queryKey: ["engineering", projectId, query, language, minFiles, maxFiles],
    queryFn: () =>
      api.engineering(projectId, {
        q: query,
        language,
        min_files: minFiles,
        max_files: maxFiles,
        scope: "all",
        page_size: 200,
      }),
    enabled: Boolean(projectId),
  });
  const claims = useQuery({ queryKey: ["claims"], queryFn: api.claims, refetchInterval: 5000 });
  const claimHistory = useQuery({
    queryKey: ["usage-records", claimTarget ? engineeringId(claimTarget) : ""],
    queryFn: () => api.usageRecords(engineeringId(claimTarget ?? { name: "" })),
    enabled: Boolean(claimTarget),
  });

  const claim = useMutation({
    mutationFn: (item: EngineeringRow) =>
      api.claim(projectId, engineeringId(item)),
    onSuccess: () => {
      toast("工程已领取，ZIP 校验通过", "ok");
      setClaimTarget(null);
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      toast(errorText(error), "error");
      void queryClient.invalidateQueries({ queryKey: ["engineering"] });
    },
  });
  const toggleDisabled = useMutation({
    mutationFn: (item: EngineeringRow) =>
      api.setEngineeringDisabled(projectId, engineeringId(item), !item.disabled, item.name),
    onSuccess: (result, item) => {
      toast(result.disabled ? `已禁用「${item.name}」，生产计划不再领取` : `已重新启用「${item.name}」`, "ok");
      void queryClient.invalidateQueries({ queryKey: ["engineering"] });
      void queryClient.invalidateQueries({ queryKey: ["intake"] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const release = useMutation({
    mutationFn: (claimId: string) => api.releaseClaim(claimId),
    onSuccess: () => {
      toast("工程已归还", "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => {
      toast(errorText(error), "error");
      void queryClient.invalidateQueries();
    },
  });
  const create = useMutation({
    mutationFn: () => api.createTask({ staging_id: stagingTarget?.engineeringId ?? "", task_name: taskName }),
    onSuccess: (result) => {
      toast("任务已创建，开始写题面", "ok");
      setStagingTarget(null);
      void navigate(`/tasks/${result.task_id}?tab=prompt`);
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const login = useMutation({
    mutationFn: () => api.sourceLogin(username, password),
    onSuccess: () => {
      toast("工程台登录成功", "ok");
      setLoginOpen(false);
      setPassword("");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const selected = projects.data?.items.find((project) => project.id === projectId);
  const authError = projects.error instanceof ApiError && projects.error.status === 401;
  const engineeringRows = useMemo(() => {
    let rows = [...(engineering.data?.items ?? [])];
    if (unusedOnly) rows = rows.filter((item) => !(item.usage?.used ?? 0));
    if (sortBy) rows.sort((left, right) => (right.usage?.[sortBy] ?? 0) - (left.usage?.[sortBy] ?? 0));
    return rows;
  }, [engineering.data?.items, sortBy, unusedOnly]);
  const allRows = engineering.data?.items ?? [];
  const disabledCount = allRows.filter((item) => item.disabled).length;
  const inUseCount = allRows.filter((item) => !item.disabled && item.holder === "mine").length;
  const takenCount = allRows.filter((item) => !item.disabled && item.holder === "other").length;
  const usableCount = allRows.length - disabledCount - takenCount;

  return (
    <div className="page source-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">SOURCE / AUTO ROTATION</span>
          <h1>题源</h1>
        </div>
        <Button onClick={() => setLoginOpen(true)}>
          <LogIn size={14} /> 工程台登录
        </Button>
      </header>

      <Panel className="project-bar" title="项目">
        <div className="project-select-row">
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">请选择项目</option>
            {projects.data?.items.map((project: ProjectRow) => (
              <option key={project.id} value={project.id}>
                {project.name}{project.usage?.used ? ` · 用 ${project.usage.used} / 交 ${project.usage.delivered} / 提 ${project.usage.submitted}` : ""}
              </option>
            ))}
          </select>
          {selected ? (
            <span>
              {selected.status ?? "active"} · 工程 {selected.engineering_count ?? "—"}
            </span>
          ) : null}
          <Button
            size="small"
            variant="ghost"
            onClick={() => void projects.refetch()}
            loading={projects.isFetching}
          >
            刷新
          </Button>
        </div>
      </Panel>

      <PromptLibraryPanel projectId={projectId} projectName={selected?.name ?? ""} />

      {authError ? (
        <EmptyState
          icon={<LogIn size={28} />}
          title="工程台会话失效，请重新登录。历史任务仍可查看。"
          action={<Button onClick={() => setLoginOpen(true)}>重新登录</Button>}
        />
      ) : null}
      {projects.isError && !authError ? <ErrorPanel error={projects.error} onRetry={() => void projects.refetch()} /> : null}

      {!authError && !projectId ? (
        <EmptyState icon={<FolderInput size={30} />} title="请先选择项目。未选择项目时不会请求工程列表。" />
      ) : !authError ? (
        <>
          <div className="table-toolbar source-filters">
            <label className="search-box">
              <Search size={14} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工程名称" />
            </label>
            <input value={language} onChange={(event) => setLanguage(event.target.value)} placeholder="语言，如 Python" />
            <input value={minFiles} onChange={(event) => setMinFiles(event.target.value)} placeholder="最少文件" inputMode="numeric" />
            <input value={maxFiles} onChange={(event) => setMaxFiles(event.target.value)} placeholder="最多文件" inputMode="numeric" />
            <span className="toolbar-count" title="生产计划默认轮转该项目下全部题目，禁用的题不再使用">
              共 {allRows.length} 题 · 可用 {usableCount}
              {inUseCount ? `（生产中 ${inUseCount}）` : ""}
              {takenCount ? ` · 他人占用 ${takenCount}` : ""}
              {disabledCount ? ` · 已禁用 ${disabledCount}` : ""}
            </span>
            <label className="check-inline"><input type="checkbox" checked={unusedOnly} onChange={(event) => setUnusedOnly(event.target.checked)} /> 未使用过（仅筛当前页）</label>
          </div>
          <p className="form-note source-note">
            题目默认全部可用，不需要手动领取：生产计划会按使用次数轮流自动领取，用完自动归还。不想用的题点「禁用」即可，随时可以重新启用。
          </p>
          {engineering.isPending ? <LoadingLines rows={6} /> : null}
          {engineering.isError ? <ErrorPanel error={engineering.error} onRetry={() => void engineering.refetch()} /> : null}
          <Panel className="table-panel density-dense">
            {engineeringRows.length ? (
              <table className="data-table source-table">
                <thead>
                  <tr>
                    <th>工程</th>
                    <th>语言</th>
                    <th className="numeric">源码文件</th>
                    <th>标签</th>
                    <th>版本</th>
                    <th className="numeric">
                      <button type="button" className="table-sort" onClick={() => setSortBy(sortBy === "used" ? "" : "used")}>用</button>
                    </th>
                    <th className="numeric">
                      <button type="button" className="table-sort" onClick={() => setSortBy(sortBy === "delivered" ? "" : "delivered")}>交</button>
                    </th>
                    <th className="numeric">
                      <button type="button" className="table-sort" onClick={() => setSortBy(sortBy === "submitted" ? "" : "submitted")}>提</button>
                    </th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {engineeringRows.map((item) => (
                    <tr key={engineeringId(item)} className={item.disabled ? "is-disabled" : undefined}>
                      <td>
                        <div className="engineering-cell">
                          {item.assets?.[0]?.id ? (
                            <img src={`/api/source/assets/${encodeURIComponent(item.assets[0].id)}`} alt="" loading="lazy" />
                          ) : <span className="asset-placeholder" />}
                          <span>
                            <strong>
                              {item.name}
                              {item.disabled ? (
                                <> <Badge tone="warn">已禁用</Badge></>
                              ) : item.holder === "mine" ? (
                                <> <Badge tone="info">生产中</Badge></>
                              ) : item.holder === "other" ? (
                                <> <Badge tone="idle">他人占用</Badge></>
                              ) : null}
                            </strong>
                            <small className="mono">{unknownText(item.github_ref ?? item.repo_url)}</small>
                          </span>
                        </div>
                      </td>
                      <td>{languageText(item)}</td>
                      <td className="numeric mono">{item.source_file_count ?? "—"}</td>
                      <td>{tagText(item)}</td>
                      <td className="mono">v{item.version_number ?? "—"}</td>
                      <td className="numeric mono">{item.usage?.used || "–"}</td>
                      <td className="numeric mono">{item.usage?.delivered || "–"}</td>
                      <td className="numeric mono">{item.usage?.submitted || "–"}</td>
                      <td>
                        <div className="row-actions">
                          <Button
                            size="small"
                            variant={item.disabled ? "primary" : "secondary"}
                            loading={toggleDisabled.isPending && toggleDisabled.variables === item}
                            title={item.disabled ? "恢复后生产计划会重新轮转到这道题" : "禁用后生产计划不再使用这道题；正在跑的任务不受影响"}
                            onClick={() => toggleDisabled.mutate(item)}
                          >
                            {item.disabled ? "启用" : "禁用"}
                          </Button>
                          {!item.holder && !item.disabled ? (
                            <Button
                              size="small"
                              variant="ghost"
                              disabled={!system.data?.source_online}
                              title="不走生产计划，手动领取后自己建题"
                              onClick={() => setClaimTarget(item)}
                            >
                              手动建题
                            </Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="当前筛选没有题目。" />
            )}
          </Panel>
        </>
      ) : null}

      <Panel className="claimed-section" title={`待开工 · ${claims.data?.items.length ?? 0}`}>
        {claims.data?.items.length ? (
          <div className="claimed-list">
            {claims.data.items.map((item) => (
              <div key={item.claimId} className="claimed-row">
                <div>
                  <strong>{item.name}</strong>
                  <small className="mono">{item.zipSha256.slice(0, 16)}…</small>
                  <small>{tagText(item)}</small>
                </div>
                <span>ZIP 已校验 · {formatDateTime(item.downloadedAt)}</span>
                <div className="row-actions">
                  <Button
                    size="small"
                    disabled={system.data?.disk_low}
                    onClick={() => {
                      setStagingTarget(item);
                      setTaskName(item.name);
                    }}
                  >
                    继续建题
                  </Button>
                  <Button
                    size="small"
                    variant="danger"
                    onClick={() => release.mutate(item.claimId)}
                    loading={release.isPending}
                  >
                    归还
                  </Button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="没有已领取未建任务的工程。" />
        )}
      </Panel>

      <Modal
        open={Boolean(claimTarget)}
        title="手动领取：领取后独占该工程"
        onClose={() => setClaimTarget(null)}
        footer={
          <>
            <Button onClick={() => setClaimTarget(null)}>取消</Button>
            <Button
              variant="primary"
              loading={claim.isPending}
              onClick={() => claimTarget && claim.mutate(claimTarget)}
            >
              确认领取
            </Button>
          </>
        }
      >
        <div className="confirm-copy">
          <strong>{claimTarget?.name}</strong>
          {claimHistory.data?.items.length ? (
            <p className="claim-history">
              已使用 {claimTarget?.usage?.used ?? claimHistory.data.items.length} 次，交付 {claimTarget?.usage?.delivered ?? 0} 次
              {claimTarget?.usage?.last_failure_kind ? `，上次失败原因：${claimTarget.usage.last_failure_kind}` : "，没有失败记录"}
            </p>
          ) : null}
          <p>领取后你在本项目内独占该工程，其他人不可见；领取有超时，管理员可回收。</p>
          <p>请确认后尽快完成建题与运行，不要囤积名额。</p>
        </div>
      </Modal>

      <Modal
        open={Boolean(stagingTarget)}
        title="创建任务"
        onClose={() => setStagingTarget(null)}
        footer={
          <>
            <Button onClick={() => setStagingTarget(null)}>取消</Button>
            <Button
              variant="primary"
              disabled={system.data?.disk_low}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              建立任务
            </Button>
          </>
        }
      >
        <Field label="任务名" hint="默认取工程名，可在建题前修改。">
          <input value={taskName} onChange={(event) => setTaskName(event.target.value)} />
        </Field>
        <div className="form-note mono">
          来源：{stagingTarget?.stagingPath}
        </div>
      </Modal>

      <Modal
        open={loginOpen}
        title="工程台登录"
        onClose={() => setLoginOpen(false)}
        footer={
          <>
            <Button onClick={() => setLoginOpen(false)}>取消</Button>
            <Button variant="primary" loading={login.isPending} onClick={() => login.mutate()}>
              登录
            </Button>
          </>
        }
      >
        <Field label="用户名">
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
        </Field>
        <Field label="密码">
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </Field>
        <div className="form-note">
          密码写入 macOS Keychain；接口只返回是否已设置。
          <a href="http://localhost:8088" target="_blank" rel="noreferrer">
            打开工程台 <ExternalLink size={12} />
          </a>
        </div>
      </Modal>
    </div>
  );
}

function engineeringId(item: EngineeringRow): string {
  return item.engineering_id ?? item.id ?? "";
}

function tagText(item: { tags?: EngineeringRow["tags"] }): string {
  const tags = item.tags ?? [];
  return tags
    .map((tag) => (
      typeof tag === "string"
        ? tag
        : [tag.parent_name, tag.name].filter(Boolean).join(" / ")
    ))
    .join(" / ") || "—";
}

function languageText(item: EngineeringRow): string {
  return formatNames(item.languages) || formatNames(item.tech_stack) || "—";
}

function unknownText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "—";
}
