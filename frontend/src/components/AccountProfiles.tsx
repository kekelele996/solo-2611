import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, LockKeyhole, Plus } from "lucide-react";
import { useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "../api/client";
import type { AccountView, ProfileDomain, ProfileDraft } from "../api/types";
import { errorText } from "../utils/display";
import { StandbyPanel } from "./StandbyPanel";
import { PasswordField } from "./PasswordField";
import { Button, ErrorPanel, Field, LoadingLines } from "./ui";

export function AccountProfiles({ token, canCreate = false, disabled = false, switchHint, onLock, details }: {
  token: string;
  canCreate?: boolean;
  disabled?: boolean;
  switchHint?: string;
  onLock?: () => void;
  details?: (items: AccountView["connections"], profile: string) => ReactNode;
}) {
  const client = useQueryClient();
  const selectorId = useId();
  const key = ["account-profiles", token];
  const catalog = useQuery({ queryKey: key, queryFn: () => api.profiles(token), gcTime: 0 });
  const [adding, setAdding] = useState(false);
  const [pending, setPending] = useState<"switch" | "create" | null>(null);
  const [error, setError] = useState("");
  if (catalog.isPending) return <LoadingLines rows={2} />;
  if (catalog.isError) return <ErrorPanel error={catalog.error} onRetry={() => void catalog.refetch()} />;
  const data = catalog.data;
  const busy = pending !== null;
  const apply = async (id: string) => {
    setPending("switch"); setError("");
    try {
      const result = await api.activateProfile(token, id);
      client.setQueryData(key, result);
      await client.invalidateQueries();
    } catch (e) { setError(errorText(e)); }
    finally { setPending(null); }
  };
  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const inputs = new FormData(event.currentTarget);
    const value = (name: string) => {
      const input = inputs.get(name);
      return typeof input === "string" ? input : "";
    };
    const draft: ProfileDraft = { name: value("name").trim(),
      solo2: { username: value("soloUser"), base_url: value("soloUrl"), locked_domain: value("domain") as ProfileDomain },
      source: { username: value("sourceUser"), base_url: value("sourceUrl") || address("题源账号") }, model_base_url: value("modelUrl") || address("模型网关"),
      secrets: { solo2_password: value("soloPassword"), source_password: value("sourcePassword"), github_token: value("github"), gateway_key: value("modelKey") },
    };
    setPending("create"); setError("");
    try { client.setQueryData(key, await api.createProfile(token, draft)); setAdding(false); }
    catch (e) { setError(errorText(e)); }
    finally { setPending(null); }
  };
  const address = (name: string) => data.connections.find(item => item.name === name)?.address ?? "";
  const primary = data.connections.find(item => item.name === "SOLO2 提交平台");
  const primaryName = data.items.find(item => item.id === data.active)?.name ?? "当前主号";
  return <div className="profile-manager">
    <div className="primary-account-group">
      <div className="profile-toolbar">
        <div className="profile-current">
          <label className="field" htmlFor={selectorId}>
            <span className="field-label">当前主号</span>
            <span className="field-control">
              <select id={selectorId} value={data.active} aria-describedby={switchHint ? `${selectorId}-hint` : undefined}
                disabled={disabled || busy || adding} onChange={e => void apply(e.target.value)}>
                {data.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </span>
          </label>
          <span className="profile-status" role="status">
            <i aria-hidden="true" />{pending === "switch" ? "正在切换…" : "正在使用"}
          </span>
          {switchHint && <small className="profile-switch-hint" id={`${selectorId}-hint`}>{switchHint}</small>}
        </div>
        <div className="form-actions">
          {canCreate && !adding && <Button disabled={disabled || busy} onClick={() => { setAdding(true); setError(""); }}>
            <Plus size={16} aria-hidden="true" />新建主号配置
          </Button>}
          {onLock && <Button variant="ghost" disabled={busy} onClick={onLock}>
            <LockKeyhole size={15} aria-hidden="true" />锁定
          </Button>}
          {!canCreate && <a href="/system#accounts" className="text-link">管理用户配置</a>}
        </div>
      </div>
      {!adding && <StandbyPanel key={JSON.stringify([token, data.active, primary?.account, primary?.address, data.standby?.revision])}
        token={token} primaryId={data.active} primaryName={primaryName} primary={primary} standby={data.standby}
        editable={canCreate} disabled={disabled || busy} />}
    </div>
    {error && <p className="profile-error danger-text" role="alert">{error}</p>}
    {adding && <form className="profile-create" onSubmit={e => void create(e)}><fieldset disabled={busy} className="profile-fields">
      <Field label="配置名称"><input name="name" required maxLength={60} autoFocus /></Field>
      <Field label="SOLO2 平台地址"><input type="url" name="soloUrl" defaultValue={address("SOLO2 提交平台")} required /></Field>
      <Field label="SOLO2 账号"><input name="soloUser" autoComplete="off" /></Field>
      <PasswordField label="SOLO2 密码" name="soloPassword" />
      <Field label="绑定领域"><select name="domain">{["", "数据工程", "安全", "游戏、图形与多媒体"].map(domain => <option key={domain} value={domain}>{domain || "未绑定"}</option>)}</select></Field>
      <PasswordField label="GitHub 凭据" name="github" />
      <Field label="题源地址"><input type="url" name="sourceUrl" defaultValue={address("题源账号")} required /></Field>
      <Field label="题源用户名"><input name="sourceUser" autoComplete="off" /></Field>
      <PasswordField label="题源密码" name="sourcePassword" />
      <Field label="模型网关地址"><input type="url" name="modelUrl" defaultValue={address("模型网关")} required /></Field>
      <PasswordField label="团队密钥" name="modelKey" />
      <div className="form-actions"><Button variant="primary" type="submit" loading={busy}>保存用户配置</Button><Button onClick={() => { setAdding(false); setError(""); }}>取消</Button></div>
    </fieldset></form>}
    <fieldset className="profile-details" disabled={busy || adding}>
      {details?.(data.connections, data.active)}
    </fieldset>
  </div>;
}
