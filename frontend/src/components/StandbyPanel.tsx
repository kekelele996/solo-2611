import { useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { AccountView, ProfileCatalog, StandbyCheck, StandbyDraft } from "../api/types";
import { errorText } from "../utils/display";
import { PasswordField } from "./PasswordField";
import { useToast } from "./toast-context";
import { Button, Field } from "./ui";

type Props = {
  token: string; primaryId: string; primaryName: string; primary?: AccountView["connections"][number];
  standby: ProfileCatalog["standby"]; editable: boolean; disabled: boolean;
};

export function StandbyPanel({ token, primaryId, primaryName, primary, standby, editable, disabled }: Props) {
  const cache = useQueryClient();
  const { toast } = useToast();
  const heading = useId();
  const form = useRef<HTMLFormElement>(null);
  const [editing, setEditing] = useState(false);
  const [pending, setPending] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<StandbyCheck | null>(null);
  const [error, setError] = useState("");
  const [username, setUsername] = useState(standby?.username ?? "");
  const keepCredentials = !!standby && username.trim() === standby.username;
  const busy = disabled || pending !== null;
  const resetFeedback = () => { setError(""); setResult(null); };

  const draftFor = (element?: HTMLFormElement): StandbyDraft | null => {
    if (!primary?.account || !primary.address) return null;
    const binding = { primary_username: primary.account, primary_address: primary.address, revision: standby?.revision ?? "" };
    if (!element) return standby ? {
      ...binding, username: standby.username, domain: standby.domain, password: "", github_token: "",
    } : null;
    const fields = new FormData(element);
    const password = fields.get("password");
    const github = fields.get("github");
    return {
      ...binding,
      username: username.trim(),
      domain: fields.get("domain") as StandbyDraft["domain"],
      password: typeof password === "string" ? password : "",
      github_token: typeof github === "string" ? github : "",
    };
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const draft = draftFor(event.currentTarget);
    if (!draft || busy) return;
    const queryKey = ["account-profiles", token];
    setPending("save"); resetFeedback();
    try {
      const saved = await api.saveStandby(token, primaryId, draft);
      cache.setQueryData<ProfileCatalog>(queryKey, current => {
        const connection = current?.connections.find(item => item.name === "SOLO2 提交平台");
        return current?.active === primaryId && (current.standby?.revision ?? "") === draft.revision
          && connection?.account === draft.primary_username && connection.address === draft.primary_address ? saved : current;
      });
      setEditing(false);
      toast("副号已保存", "ok");
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setPending(null);
      void cache.invalidateQueries({ queryKey });
    }
  };

  const test = async () => {
    const element = form.current;
    if (busy || (editing && !element?.reportValidity())) return;
    const draft = draftFor(element ?? undefined);
    if (!draft) return;
    setPending("test"); resetFeedback();
    try { setResult(await api.testStandby(token, primaryId, draft)); }
    catch (reason) { setError(errorText(reason)); }
    finally { setPending(null); }
  };
  return <section className="standby-panel" aria-labelledby={heading}>
    <div className="standby-heading">
      <div><h3 id={heading}>{primary?.account || primaryName} 的副号</h3>
        <p>{standby ? `${standby.username} · ${standby.domain}` : "尚未绑定副号"}</p>
      </div>
      {editable && !editing && <div className="form-actions">
        {standby && <Button size="small" disabled={busy} loading={pending === "test"} onClick={() => void test()}>测试副号连接</Button>}
        <Button size="small" disabled={busy || !primary?.account}
          onClick={() => { setUsername(standby?.username ?? ""); resetFeedback(); setEditing(true); }}>
          {standby ? "编辑副号" : "绑定副号"}
        </Button>
      </div>}
    </div>
    {editing && <form ref={form} onChange={resetFeedback} onSubmit={e => void save(e)}>
      <fieldset className="profile-fields" disabled={busy}>
        <Field label="副号 SOLO2 账号"><input autoFocus required maxLength={200} autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} /></Field>
        <Field label="副号绑定领域"><select name="domain" defaultValue={standby?.domain ?? primary?.domain ?? ""} required>
          <option value="">请选择领域</option>{["数据工程", "安全", "游戏、图形与多媒体"].map(domain => <option key={domain}>{domain}</option>)}
        </select></Field>
        <PasswordField label="副号 SOLO2 密码" name="password" required={!keepCredentials} placeholder={keepCredentials ? "留空保留已保存密码" : undefined} />
        <PasswordField label="副号专属 GitHub 密钥" name="github" required={!keepCredentials} placeholder={keepCredentials ? "留空保留已保存密钥" : undefined} />
        <div className="form-actions">
          <Button variant="primary" type="submit" loading={pending === "save"}>保存副号</Button>
          <Button loading={pending === "test"} onClick={() => void test()}>测试副号连接</Button>
          <Button onClick={() => { setEditing(false); resetFeedback(); }}>取消</Button>
        </div>
      </fieldset>
    </form>}
    {error && <p role="alert" className="danger-text">{error}</p>}
    {result && <div className="standby-results" role="status">
      <span className={`test-result ${result.solo2.ok ? "is-ok" : "is-bad"}`}>{result.solo2.message}</span>
      <span className={`test-result ${result.github.ok ? "is-ok" : "is-bad"}`}>{result.github.message}</span>
    </div>}
  </section>;
}
