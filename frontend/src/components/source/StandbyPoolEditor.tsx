import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import type { EngineeringPools, ProjectRow, StandbySource } from "../../api/types";
import { errorText } from "../../utils/display";
import { AccountViewForm } from "../AccountViewPanel";
import { useToast } from "../toast-context";
import { Button, ErrorPanel, Field, LoadingLines } from "../ui";

type Props = { primaryId: string; projects: ProjectRow[]; onClose: () => void };

export function StandbyPoolEditor({ onClose, ...props }: Props) {
  const [session, setSession] = useState<{ token: string; seconds: number } | null>(null);
  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => setSession(null), session.seconds * 1000);
    return () => { window.clearTimeout(timer); void api.lockAccounts(session.token).catch(() => {}); };
  }, [session]);

  return <div className="source-pool-editor" id="standby-project-editor">
    {session ? <PoolSettings {...props} onClose={onClose} token={session.token} /> : (
      <AccountViewForm onUnlock={view => { if (view.token) setSession({ token: view.token, seconds: view.expires_in }); }} />
    )}
  </div>;
}

function PoolSettings(props: Props & { token: string }) {
  const { token } = props;
  const profiles = useQuery({ queryKey: ["source-pool-profile", token], queryFn: () => api.profiles(token), gcTime: 0 });
  const profile = profiles.data?.active ?? "";
  const saved = useQuery({ queryKey: ["engineering-pools", profile, token], queryFn: () => api.engineeringPools(token, profile),
    enabled: Boolean(profile && profiles.data?.standby), gcTime: 0 });

  if (profiles.isError) return <ErrorPanel error={profiles.error} onRetry={() => void profiles.refetch()} />;
  if (!profiles.data) return <LoadingLines rows={2} />;
  if (!profiles.data.standby) return <p className="form-note">请先到 <Link className="text-link" to="/system#accounts">账号与连接</Link> 保存 SOLO2 副号和 GitHub 凭据。</p>;
  if (saved.isError) return <ErrorPanel error={saved.error} onRetry={() => void saved.refetch()} />;
  if (!saved.data) return <LoadingLines rows={2} />;
  return <PoolForm {...props} profile={profile} saved={saved.data} />;
}

function PoolForm({ token, profile, primaryId, projects, onClose, saved }: Props & {
  token: string; profile: string; saved: EngineeringPools;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const configuredAddress = saved.primary_source_address ?? "";
  const initialAddress = saved.standby_use_primary_source
    ? configuredAddress
    : saved.standby_source_address || configuredAddress;
  const [project, setProject] = useState(saved.standby_project_id ?? "");
  const [source, setSource] = useState<StandbySource>({
    standby_use_primary_source: false,
    standby_source_address: initialAddress,
    standby_source_user: saved.standby_source_user ?? "",
    standby_source_password: saved.standby_source_password ?? "",
  });
  const load = useMutation({
    mutationFn: () => api.standbyProjects(token, profile, source),
    onSuccess: ({ items }) => setProject(current => items.some(item => item.id === current) ? current : ""),
  });
  const options = useMemo(() => {
    const savedProject = saved.standby_project_id
      ? [{ id: saved.standby_project_id, name: saved.standby_project_name || saved.standby_project_id }]
      : [];
    const sharedAddress = sameOrigin(source.standby_source_address, configuredAddress);
    const merged = [...savedProject, ...(load.data?.items ?? []), ...(sharedAddress ? projects : [])]
      .map(item => ({ id: item.id, name: item.name }));
    return [...new Map(merged.map(item => [item.id, item])).values()].filter(item => item.id !== primaryId);
  }, [configuredAddress, load.data?.items, primaryId, projects, saved.standby_project_id, saved.standby_project_name,
    source.standby_source_address]);
  const sameAsSaved = source.standby_source_address === saved.standby_source_address
    && source.standby_source_user === saved.standby_source_user;
  const credentialsReady = Boolean(source.standby_source_address.trim() && source.standby_source_user.trim()
    && (source.standby_source_password || (saved.standby_source_password && sameAsSaved)));
  const canSave = Boolean(primaryId && project && options.some(item => item.id === project) && credentialsReady);
  const save = useMutation({
    mutationFn: () => api.saveEngineeringPools(token, profile, {
      ...source, standby_use_primary_source: false, primary_project_id: primaryId, standby_project_id: project,
    }),
    onSuccess: () => {
      toast("副号项目池已保存", "ok");
      void queryClient.invalidateQueries({ queryKey: ["intake"] });
      void queryClient.invalidateQueries({ queryKey: ["dispatch"] });
      void queryClient.invalidateQueries({ queryKey: ["dispatch-upcoming"] });
      onClose();
    },
  });
  function update<K extends keyof StandbySource>(key: K, value: StandbySource[K]) {
    setSource(current => {
      const next = { ...current, [key]: value };
      if ((key === "standby_source_address" && value !== saved.standby_source_address)
        || (key === "standby_source_user" && value !== saved.standby_source_user)) {
        next.standby_source_password = "";
      }
      return next;
    });
    setProject("");
    load.reset();
    save.reset();
  }

  return <form onSubmit={event => { event.preventDefault(); if (canSave) save.mutate(); }}>
    <fieldset className="source-pool-fields" disabled={load.isPending || save.isPending}>
      <div className="source-pool-connection">
        <Field label="工程台地址" hint="默认使用「设置 → 账号与连接」里的题源地址，可改为其他工程台。">
          <input type="url" value={source.standby_source_address} required placeholder={configuredAddress}
            onChange={event => update("standby_source_address", event.target.value)} /></Field>
        <Field label="副号工程台账号" hint="主号和副号必须是两个不同的账号。">
          <input value={source.standby_source_user} required autoComplete="off"
            onChange={event => update("standby_source_user", event.target.value)} /></Field>
        <Field label="副号工程台密码" hint="明文展示并回填，仅用于读取副号项目组。">
          <input type="text" autoComplete="off" spellCheck={false} autoCapitalize="none"
            value={source.standby_source_password} required
            placeholder="请输入副号工程台密码"
            onChange={event => update("standby_source_password", event.target.value)} /></Field>
      </div>
      <div className="source-pool-controls">
        <Field label="副号项目组">
          <select value={options.some(item => item.id === project) ? project : ""} required disabled={!options.length}
            onChange={event => { setProject(event.target.value); save.reset(); }}>
            <option value="">{!load.data ? "先读取项目" : options.length ? "请选择项目组" : "暂无其他项目组"}</option>
            {options.map(({ id, name }) => <option key={id} value={id}>{name}</option>)}
          </select>
        </Field>
        <Button onClick={() => load.mutate()} loading={load.isPending} disabled={!credentialsReady}>读取项目</Button>
        <Button type="submit" variant="primary" loading={save.isPending} disabled={!canSave}>保存副号项目池</Button>
      </div>
      {(save.error || load.error) && <p className="danger-text" role="alert">{errorText(save.error || load.error)}</p>}
    </fieldset>
  </form>;
}

function sameOrigin(left: string, right: string): boolean {
  try {
    return new URL(left).origin === new URL(right).origin;
  } catch {
    return false;
  }
}
