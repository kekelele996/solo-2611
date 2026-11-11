import { AlertTriangle, Check, CircleX, Info, X } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useMemo, useState } from "react";

import { ToastContext, type ToastTone } from "./toast-context";

type ToastItem = { id: number; message: string; tone: ToastTone };

const VISIBLE = 3;
const icons = { ok: Check, info: Info, warn: AlertTriangle, error: CircleX } as const;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const toast = useCallback((message: string, tone: ToastTone = "info") => {
    const id = Date.now() + Math.random();
    setItems((current) => [
      ...current.filter((item) => item.message !== message || item.tone !== tone),
      { id, message, tone },
    ]);
    // 错误原文要来得及读完，停留时间加倍
    window.setTimeout(
      () => setItems((current) => current.filter((item) => item.id !== id)),
      tone === "error" ? 8000 : 4000,
    );
  }, []);
  const value = useMemo(() => ({ toast }), [toast]);
  const visible = items.slice(-VISIBLE);
  const hidden = items.length - visible.length;
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.length ? (
          <button type="button" className="toast-clear" onClick={() => setItems([])}>
            全部清除
          </button>
        ) : null}
        {hidden > 0 ? <span className="toast-more">还有 {hidden} 条</span> : null}
        {visible.map((item) => {
          const Icon = icons[item.tone];
          return (
            <div key={item.id} className={`toast toast-${item.tone}`} role={item.tone === "error" ? "alert" : "status"}>
              <Icon size={14} aria-hidden="true" />
              <span>{item.message}</span>
              <button
                type="button"
                className="icon-button"
                onClick={() => setItems((current) => current.filter((entry) => entry.id !== item.id))}
                aria-label="关闭提示"
              >
                <X size={13} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
