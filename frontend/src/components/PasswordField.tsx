import { Eye, EyeOff } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import type { RevealTarget } from "../api/types";
import { errorText } from "../utils/display";
import { Button } from "./ui";

type SavedPassword = { token: string; profile: string; target: RevealTarget };

type Props = {
  label: string; name?: string; hint?: string; placeholder?: string; readOnly?: boolean; required?: boolean;
  value?: string; onChange?: (value: string) => void; saved?: SavedPassword;
};

export function PasswordField(props: Props) {
  // 更换账号、连接或解锁会话时丢弃明文和正在等待的响应。
  const { saved } = props;
  return <PasswordControl key={saved ? `${saved.profile}/${saved.target}/${saved.token}` : "draft"} {...props} />;
}

function PasswordControl({ label, name, hint, placeholder, readOnly, required, value, onChange, saved }: Props) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const [visible, setVisible] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  const entered = value ?? draft;
  const showing = visible && (revealed !== null || entered !== "");

  useEffect(() => {
    const hide = () => {
      if (document.visibilityState === "hidden") {
        pending.current?.abort();
        setBusy(false); setVisible(false); setRevealed(null);
      }
    };
    document.addEventListener("visibilitychange", hide);
    return () => { pending.current?.abort(); document.removeEventListener("visibilitychange", hide); };
  }, []);

  const toggle = async () => {
    setError("");
    if (showing || entered) {
      setRevealed(null); setVisible(!showing);
      return;
    }
    if (!saved) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    try {
      const result = await api.revealPassword(saved.token, saved.profile, saved.target, controller.signal);
      if (!controller.signal.aborted) { setRevealed(result.password); setVisible(true); }
    } catch (e) {
      if (!controller.signal.aborted) setError(errorText(e));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const action = `${showing ? "隐藏" : "显示"}${label}`;
  return <div className="field password-field">
    <label htmlFor={id} className="field-label">{label}</label>
    <div className="field-control password-control">
      <input id={id} name={name} type={showing ? "text" : "password"} value={revealed ?? entered}
        readOnly={readOnly} required={required} placeholder={placeholder} autoComplete={readOnly || saved ? "off" : "new-password"}
        spellCheck={false} autoCapitalize="none" aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={e => {
          const next = e.target.value;
          pending.current?.abort(); setBusy(false); setRevealed(null); setError("");
          setVisible(showing && next !== "");
          if (onChange) onChange(next); else setDraft(next);
        }} />
      <Button size="small" variant="ghost" aria-label={action} title={action} aria-controls={id}
        disabled={!saved && !entered} loading={busy} onClick={() => void toggle()}>
        {!busy && (showing ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />)}
      </Button>
    </div>
    {hint && <small id={`${id}-hint`}>{hint}</small>}
    {error && <small role="alert">{error}</small>}
  </div>;
}
