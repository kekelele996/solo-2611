import { AlertTriangle, Check, Clipboard, LoaderCircle, X } from "lucide-react";
import type { ButtonHTMLAttributes, CSSProperties, HTMLAttributes, ReactNode } from "react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import type { Step } from "../api/types";
import { errorText, shortDetail, statusTone } from "../utils/display";
import type { Tone } from "../utils/display";
import { useToast } from "./toast-context";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

export function Button({
  children,
  variant = "secondary",
  size = "default",
  loading = false,
  className = "",
  disabled,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "default" | "small";
  loading?: boolean;
}) {
  return (
    <button
      type={type}
      className={`button button-${variant} button-${size} ${className}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <LoaderCircle className="spin-icon" size={14} aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export function StatusDot({
  state,
  label,
  pulse,
}: {
  state: string;
  label?: string;
  /** 呼吸光晕只留给「此刻真的在跑」的条目；计数器之类的汇总点传 false。 */
  pulse?: boolean;
}) {
  const breathing = pulse ?? state === "running";
  return (
    <span className={`status-dot status-${statusTone(state)}`} data-state={state} data-pulse={breathing || undefined}>
      <i aria-hidden="true" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}

export function Badge({ children, tone = "idle" }: { children: ReactNode; tone?: Tone }) {
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
  title = "请求没有完成",
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div className="error-panel" role="alert">
      <AlertTriangle size={16} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <pre>{errorText(error)}</pre>
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
  warnAt,
  min,
}: {
  value: number;
  max: number;
  label?: string;
  tone?: "auto" | Tone;
  /** 超过即变黄；缺省为 max 的 75%。 */
  warnAt?: number;
  /** 低于即变红（例如 Feedback 的最少字数）。 */
  min?: number;
}) {
  const ratio = max <= 0 ? 0 : Math.min(1, Math.max(0, value / max));
  const warnLine = warnAt ?? max * 0.75;
  const resolved =
    tone === "auto"
      ? value > max || (min !== undefined && value < min)
        ? "danger"
        : value > warnLine
          ? "warn"
          : "ok"
      : tone;
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
        <span style={{ width: `${ratio * 100}%` }} />
        {warnAt !== undefined && max > 0 ? <i style={{ left: `${(warnAt / max) * 100}%` }} aria-hidden="true" /> : null}
        {min !== undefined && max > 0 ? <i style={{ left: `${(min / max) * 100}%` }} aria-hidden="true" /> : null}
      </div>
    </div>
  );
}

export function StepRail({ steps, onSelect }: { steps: Step[]; onSelect?: (step: Step) => void }) {
  const current = steps.find((step) => !step.ok);
  return (
    <nav className="step-rail" aria-label="交付完整性 11 步">
      {steps.map((step) => {
        const isCurrent = step.number === current?.number;
        return (
          <button
            type="button"
            key={step.number}
            className={`step-row step-${step.state}`}
            data-current={isCurrent}
            aria-current={isCurrent ? "step" : undefined}
            title={step.detail ? `${step.name}：${step.detail}` : step.name}
            onClick={() => onSelect?.(step)}
          >
            <span className="step-index">{step.ok ? <Check size={10} strokeWidth={3} aria-label="已通过" /> : step.number}</span>
            <span className="step-name">{step.name}</span>
            <span className="step-detail">{shortDetail(step.detail)}</span>
          </button>
        );
      })}
    </nav>
  );
}

const SNAP_POINTS = [264, 320, 400];
const LEFT_RANGE = [220, 400] as const;
const RIGHT_RANGE = [280, 480] as const;
const PANE_KEY = "solo-auto-panes";

function readPanes(): { left: number; right: number } {
  try {
    const stored = JSON.parse(window.localStorage.getItem(PANE_KEY) ?? "{}") as { left?: number; right?: number };
    return {
      left: clamp(Number(stored.left) || 264, LEFT_RANGE),
      right: clamp(Number(stored.right) || 320, RIGHT_RANGE),
    };
  } catch {
    return { left: 264, right: 320 };
  }
}

function clamp(value: number, [low, high]: readonly [number, number]): number {
  return Math.min(high, Math.max(low, value));
}

function snap(value: number): number {
  const target = SNAP_POINTS.find((point) => Math.abs(point - value) <= 16);
  return target ?? value;
}

export function SplitPane({
  left,
  center,
  right,
  rightCollapsed = false,
}: {
  left: ReactNode;
  center: ReactNode;
  right: ReactNode;
  rightCollapsed?: boolean;
}) {
  const [panes, setPanes] = useState(readPanes);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (dragging) return;
    try {
      window.localStorage.setItem(PANE_KEY, JSON.stringify(panes));
    } catch {
      // 存不下就只在本次会话内生效
    }
  }, [dragging, panes]);

  const gutter = (side: "left" | "right") => {
    const range = side === "left" ? LEFT_RANGE : RIGHT_RANGE;
    const fallback = side === "left" ? 264 : 320;
    // 右栏的分隔条在它的左缘：指针右移 = 变窄
    const direction = side === "left" ? 1 : -1;
    const update = (width: number) => setPanes((current) => ({ ...current, [side]: clamp(width, range) }));
    return (
      <div
        className="pane-gutter"
        role="separator"
        aria-orientation="vertical"
        aria-label={side === "left" ? "调整步骤栏宽度" : "调整证据栏宽度"}
        aria-valuenow={panes[side]}
        aria-valuemin={range[0]}
        aria-valuemax={range[1]}
        tabIndex={0}
        title="拖拽调整宽度，双击复位"
        onDoubleClick={() => update(fallback)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") update(panes[side] - 8 * direction);
          if (event.key === "ArrowRight") update(panes[side] + 8 * direction);
          if (event.key === "Home") update(fallback);
        }}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          const handle = event.currentTarget;
          handle.setPointerCapture(event.pointerId);
          const startX = event.clientX;
          const startWidth = panes[side];
          setDragging(true);
          const onMove = (moveEvent: PointerEvent) => update(startWidth + (moveEvent.clientX - startX) * direction);
          const onUp = () => {
            handle.removeEventListener("pointermove", onMove);
            handle.removeEventListener("pointerup", onUp);
            handle.removeEventListener("pointercancel", onUp);
            setDragging(false);
            setPanes((current) => ({ ...current, [side]: snap(current[side]) }));
          };
          handle.addEventListener("pointermove", onMove);
          handle.addEventListener("pointerup", onUp);
          handle.addEventListener("pointercancel", onUp);
        }}
      />
    );
  };

  return (
    <div
      className={`split-pane ${dragging ? "is-dragging" : ""}`}
      data-right-collapsed={rightCollapsed || undefined}
      style={{ "--pane-left": `${panes.left}px`, "--pane-right": `${rightCollapsed ? 0 : panes.right}px` } as CSSProperties}
    >
      <aside className="pane-left">
        {left}
        {gutter("left")}
      </aside>
      <main className="pane-center">{center}</main>
      <aside className="pane-right" inert={rightCollapsed}>
        {right}
        {gutter("right")}
      </aside>
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
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // 打开时把焦点送进对话框，关闭后还给触发它的元素
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node = dialog.current;
    const first = node?.querySelector<HTMLElement>("input:not([disabled]), textarea:not([disabled]), select:not([disabled])");
    (first ?? node)?.focus();
    return () => previous?.focus();
  }, [open]);

  if (!open) return null;
  return (
    <div className="modal-layer" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialog}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer>{footer}</footer> : null}
      </section>
    </div>
  );
}

/** 需要二次确认的操作统一走这里，不用浏览器原生 confirm/prompt。 */
export function ConfirmModal({
  open,
  title,
  children,
  confirmLabel,
  tone = "primary",
  loading = false,
  disabled = false,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  loading?: boolean;
  disabled?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant={tone} loading={loading} disabled={disabled} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="confirm-copy">{children}</div>
    </Modal>
  );
}

export function CopyButton({ value, label, size = 13 }: { value: string; label: string; size?: number }) {
  const { toast } = useToast();
  return (
    <button
      type="button"
      className="icon-button copy-button"
      aria-label={`复制${label}`}
      title={`复制${label}`}
      onClick={(event) => {
        event.stopPropagation();
        navigator.clipboard.writeText(value).then(
          () => toast(`${label}已复制`, "info"),
          (error: unknown) => toast(`复制失败：${errorText(error)}`, "error"),
        );
      }}
    >
      <Clipboard size={size} />
    </button>
  );
}

export function CodeBlock({
  children,
  title,
  maxHeight = 480,
  tail = false,
  actions,
}: {
  children: string;
  title?: string;
  maxHeight?: number;
  /** 日志模式：内容更新时，如果本来就停在底部，就继续贴着底部。 */
  tail?: boolean;
  actions?: ReactNode;
}) {
  const scroller = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);
  useLayoutEffect(() => {
    const node = scroller.current;
    if (tail && node && pinned.current) node.scrollTop = node.scrollHeight;
  }, [children, tail]);
  return (
    <div className="code-block" style={{ maxHeight }}>
      {title ? (
        <header>
          <span>{title}</span>
          <span className="code-block-actions">
            {actions}
            <CopyButton value={children} label="内容" size={14} />
          </span>
        </header>
      ) : null}
      <pre
        ref={scroller}
        tabIndex={0}
        onScroll={(event) => {
          const node = event.currentTarget;
          pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
        }}
      >
        {children}
      </pre>
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
  hint?: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <span className="field-control">{children}</span>
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

export function KeyValue({
  label,
  value,
  mono = false,
  copy,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
  /** 传入完整值即在行尾给出复制按钮（悬停/聚焦时出现）。 */
  copy?: string | null;
}) {
  return (
    <div className="key-value">
      <span>{label}</span>
      <strong className={mono ? "mono" : ""} title={typeof value === "string" ? value : undefined}>
        {value || "—"}
      </strong>
      {copy ? <CopyButton value={copy} label={label} size={12} /> : null}
    </div>
  );
}

export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: ReadonlyArray<readonly [T, string]>;
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [bar, setBar] = useState({ x: 0, width: 0 });
  useLayoutEffect(() => {
    const node = buttons.current.get(value);
    if (node) setBar({ x: node.offsetLeft, width: node.offsetWidth });
  }, [value, tabs]);
  return (
    <div className="task-tabs" role="tablist" aria-label={label}>
      {tabs.map(([key, text], index) => (
        <button
          type="button"
          key={key}
          role="tab"
          ref={(node) => {
            if (node) buttons.current.set(key, node);
            else buttons.current.delete(key);
          }}
          aria-selected={value === key}
          tabIndex={value === key ? 0 : -1}
          className={value === key ? "is-active" : ""}
          onClick={() => onChange(key)}
          onKeyDown={(event) => {
            const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
            if (!step) return;
            event.preventDefault();
            const next = tabs[(index + step + tabs.length) % tabs.length][0];
            onChange(next);
            buttons.current.get(next)?.focus();
          }}
        >
          <kbd>{index + 1}</kbd>
          {text}
        </button>
      ))}
      {/* 下划线只做位移：1px 宽的条用 scaleX 撑到目标宽度，不触发布局 */}
      <span
        className="tab-indicator"
        aria-hidden="true"
        style={{ transform: `translateX(${bar.x}px) scaleX(${bar.width})` }}
      />
    </div>
  );
}

export function Panel({
  title,
  actions,
  children,
  className = "",
  ...props
}: Omit<HTMLAttributes<HTMLElement>, "title"> & {
  title?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={`panel ${className}`} {...props}>
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
