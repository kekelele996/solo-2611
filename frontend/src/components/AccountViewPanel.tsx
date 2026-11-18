import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { AccountView, PasswordTarget } from "../api/types";
import { errorText } from "../utils/display";
import { AccountProfiles } from "./AccountProfiles";
import { PasswordField } from "./PasswordField";
import { Button, ErrorPanel, Field, LoadingLines } from "./ui";

const credentialLabels: Record<PasswordTarget, string> = { solo2: "SOLO2 密码", source: "题源密码", gateway: "团队密钥" };

export function AccountViewPanel() {
  const [open, setOpen] = useState(true);
  return <section className="dispatch-fold">
    <button type="button" aria-expanded={open} aria-controls="dispatch-accounts" onClick={() => setOpen(!open)}>
      <strong>账号与连接</strong><span>查看码</span><ChevronDown size={16} />
    </button>
    {open && <div id="dispatch-accounts"><AccountViewForm /></div>}
  </section>;
}

export function AccountViewForm({ onUnlock }: { onUnlock?: (view: AccountView) => void }) {
  const client = useQueryClient();
  const status = useQuery({ queryKey: ["account-view-status"], queryFn: api.viewCodeStatus, staleTime: 0 });
  const [code, setCode] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [change, setChange] = useState(false);
  const [view, setView] = useState<AccountView | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!view) return;
    const timer = window.setTimeout(() => { setView(null); setMessage("查看已到期，请重新输入查看码"); }, view.expires_in * 1000);
    return () => { window.clearTimeout(timer); if (view.token) void api.lockAccounts(view.token).catch(() => {}); };
  }, [view]);

  if (status.isPending) return <LoadingLines rows={2} />;
  if (status.isError) return <ErrorPanel error={status.error} onRetry={() => void status.refetch()} />;
  const configured = status.data.configured;
  const setting = !configured || change;
  const resetInputs = () => { setCode(""); setNext(""); setConfirm(""); setError(""); };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError(""); setMessage("");
    if (setting && confirm !== next) { setError("两次输入的新查看码不一致"); return; }
    setBusy(true);
    try {
      if (setting) {
        await api.setViewCode(code, next);
        await Promise.all([status.refetch(), client.invalidateQueries({ queryKey: ["config"] })]);
        if (!mounted.current) return;
        setChange(false);
        setMessage("查看码已保存，请输入查看码解锁");
      } else {
        const result = await api.unlockAccounts(code, onUnlock ? "settings" : "profiles");
        if (!mounted.current) return;
        if (onUnlock) onUnlock(result); else setView(result);
      }
      resetInputs();
    } catch (reason) {
      if (mounted.current) { setCode(""); setError(errorText(reason)); void status.refetch(); }
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  if (view) return <div className="account-view">
    {view.token && <AccountProfiles token={view.token} details={(items, profile) => items.map(item => <section key={`${profile}/${item.name}`}>
      <h3>{item.name}</h3>
      <dl>{[["账号", item.account], ["地址", item.address], ["绑定领域", item.domain], ["凭据", item.credential_saved ? "已保存" : "未设置"]]
        .filter(([, value]) => value != null).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "未设置"}</dd></div>)}</dl>
      {view.token && item.password_target && item.credential_saved && <PasswordField
        label={credentialLabels[item.password_target]} readOnly placeholder="••••••••"
        saved={{ token: view.token, profile, target: item.password_target }} />}
    </section>)} />}
    <Button size="small" onClick={() => { setView(null); setMessage(""); }}>锁定</Button>
  </div>;

  return <form className="account-view" onSubmit={e => void submit(e)}>
    {configured && <Field label={change ? "当前查看码" : "查看码"}><input type="password" autoComplete="off" maxLength={128} required value={code} onChange={e => setCode(e.target.value)} /></Field>}
    {setting && <>
      <Field label="新查看码" hint="至少 6 个字符"><input type="password" autoComplete="new-password" minLength={6} maxLength={128} required value={next} onChange={e => setNext(e.target.value)} /></Field>
      <Field label="确认新查看码"><input type="password" autoComplete="new-password" maxLength={128} required value={confirm} onChange={e => setConfirm(e.target.value)} /></Field>
    </>}
    {error && <p role="alert" className="danger-text">{error}</p>}
    {message && <p role="status" className="form-note">{message}</p>}
    <div className="form-actions">
      <Button type="submit" size="small" variant="primary" loading={busy}>{setting ? "保存查看码" : "解锁查看"}</Button>
      {configured && <Button size="small" variant="ghost" disabled={busy} onClick={() => { resetInputs(); setMessage(""); setChange(!change); }}>{change ? "取消" : "修改查看码"}</Button>}
    </div>
  </form>;
}
