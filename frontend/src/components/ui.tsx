import {
  AlertTriangle,
  Clipboard,
  LoaderCircle,
  X,
} from "lucide-react";
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";
import { useEffect, useId, useState } from "react";

import type { Step } from "../api/types";
import { useToast } from "./toast-context";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

export function Button({
  children,
  variant = "secondary",
  size = "default",
  loading = false,
  className = "",
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "default" | "small";
  loading?: boolean;
}) {
  return (
    <button
      className={`button button-${variant} button-${size} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <LoaderCircle className="spin-icon" size={14} aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export function StatusDot({ state, label }: { state: string; label?: string }) {
  const tone =
    state === "running" || state === "done" || state === "exported" || state === "ready"
      ? "ok"
      : state === "blocked" || state === "failed" || state === "orphan"
        ? "danger"
        : state === "ran" || state === "recorded" || state === "prompt_ok"
          ? "warn"
          : "idle";
  return (
    <span className={`status-dot status-${tone}`} data-state={state}>
      <i aria-hidden="true" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}

export function Badge({
  children,
  tone = "idle",
}: {
  children: ReactNode;
  tone?: "ok" | "warn" | "danger" | "info" | "idle";
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function EmptyState({
  icon,
  title,
  action,
}: {
  icon?: ReactNode;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <p>{title}</p>
      {action}
    </div>
  );
}

export function LoadingLines({ rows = 3 }: { rows?: number }) {
  return (
    <div className="loading-lines" aria-label="加载中">
      {Array.from({ length: rows }, (_, index) => (
        <span key={index} />
      ))}
    </div>
  );
}

export function ErrorPanel({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="error-panel" role="alert">
      <AlertTriangle size={16} />
      <div>
        <strong>请求没有完成</strong>
        <pre>{message}</pre>
      </div>
      {onRetry ? (
        <Button size="small" onClick={onRetry}>
          重试
        </Button>
      ) : null}
    </div>
  );
}

export function Meter({
  value,
  max,
  label,
  tone = "auto",
}: {
  value: number;
  max: number;
  label?: string;
  tone?: "auto" | "ok" | "warn" | "danger" | "info";
}) {
  const rawRatio = max <= 0 ? 0 : value / max;
  const ratio = Math.min(1, Math.max(0, rawRatio));
  const resolved =
    tone === "auto" ? (rawRatio > 1 ? "danger" : rawRatio > 0.75 ? "warn" : "ok") : tone;
  return (
    <div className={`meter meter-${resolved}`}>
      {label ? (
        <div className="meter-head">
          <span>{label}</span>
          <span className="mono">
            {value}/{max}
          </span>
        </div>
      ) : null}
      <div className="meter-track">
        <span style={{ width: `${Math.min(100, ratio * 100)}%` }} />
      </div>
    </div>
  );
}

export function StepRail({ steps }: { steps: Step[] }) {
  const current = steps.find((step) => !step.ok);
  return (
    <nav className="step-rail" aria-label="交付完整性 11 步">
      {steps.map((step) => (
        <div
          key={step.number}
          className={`step-row step-${step.state}`}
          data-current={step.number === current?.number}
          title={step.detail || step.name}
        >
          <span className="step-index">{step.number}</span>
          <span className="step-name">{step.name}</span>
          <span className="step-detail">{step.detail}</span>
        </div>
      ))}
    </nav>
  );
}

export function SplitPane({
  left,
  center,
  right,
}: {
  left: ReactNode;
  center: ReactNode;
  right: ReactNode;
}) {
  const [leftWidth, setLeftWidth] = useState(264);
  const [rightWidth, setRightWidth] = useState(320);

  return (
    <div className="split-pane" style={{ gridTemplateColumns: `${leftWidth}px minmax(520px, 1fr) ${rightWidth}px` }}>
      <aside className="pane-left">{left}</aside>
      <div
        className="pane-gutter"
        role="separator"
        aria-orientation="vertical"
        tabIndex={0}
        onDoubleClick={() => setLeftWidth(264)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setLeftWidth((value) => Math.max(220, value - 8));
          if (event.key === "ArrowRight") setLeftWidth((value) => Math.min(360, value + 8));
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          const startX = event.clientX;
          const startWidth = leftWidth;
          const onMove = (moveEvent: PointerEvent) => {
            setLeftWidth(Math.min(360, Math.max(220, startWidth + moveEvent.clientX - startX)));
          };
          const onUp = () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
          };
          window.addEventListener("pointermove", onMove);
          window.addEventListener("pointerup", onUp);
        }}
      />
      <main className="pane-center">{center}</main>
      <div
        className="pane-gutter"
        role="separator"
        aria-orientation="vertical"
        tabIndex={0}
        onDoubleClick={() => setRightWidth(320)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setRightWidth((value) => Math.min(480, value + 8));
          if (event.key === "ArrowRight") setRightWidth((value) => Math.max(280, value - 8));
        }}
      />
      <aside className="pane-right">{right}</aside>
    </div>
  );
}

export function Modal({
  open,
  title,
  children,
  onClose,
  footer,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
}) {
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-layer" role="presentation" onMouseDown={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2 id={titleId}>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer>{footer}</footer> : null}
      </section>
    </div>
  );
}

export function CodeBlock({
  children,
  title,
  maxHeight = 480,
}: {
  children: string;
  title?: string;
  maxHeight?: number;
}) {
  const { toast } = useToast();
  return (
    <div className="code-block" style={{ maxHeight }}>
      {title ? (
        <header>
          <span>{title}</span>
          <button
            className="icon-button"
            aria-label="复制代码"
            onClick={() => {
              void navigator.clipboard.writeText(children).then(() => toast("已复制", "info"));
            }}
          >
            <Clipboard size={14} />
          </button>
        </header>
      ) : null}
      <pre>{children}</pre>
    </div>
  );
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <span className="field-control">{children}</span>
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

export function KeyValue({ label, value, mono = false }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="key-value">
      <span>{label}</span>
      <strong className={mono ? "mono" : ""}>{value || "—"}</strong>
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="tablist">
      {options.map((option) => (
        <button
          key={option.value}
          role="tab"
          aria-selected={value === option.value}
          className={value === option.value ? "is-active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Panel({ title, actions, children, ...props }: HTMLAttributes<HTMLElement> & {
  title?: string;
  actions?: ReactNode;
}) {
  return (
    <section className="panel" {...props}>
      {title || actions ? (
        <header className="panel-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions}
        </header>
      ) : null}
      {children}
    </section>
  );
}
