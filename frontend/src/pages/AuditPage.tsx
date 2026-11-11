import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ClipboardCheck, RotateCcw, ShieldAlert, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { AuditDetail } from "../api/types";
import { useToast } from "../components/toast-context";
import { Badge, Button, EmptyState, ErrorPanel, Field, LoadingLines, Modal, Panel, StatusDot } from "../components/ui";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";

export function AuditPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const selected = params.get("task") ?? "";
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [disposition, setDisposition] = useState<"redo_feedback" | "manual" | "void">("redo_feedback");
  const video = useRef<HTMLVideoElement>(null);
  const queue = useQuery({
    queryKey: ["audits", "pending"],
    queryFn: () => api.audits("pending"),
    refetchInterval: 5000,
  });
  const items = useMemo(() => queue.data?.items ?? [], [queue.data?.items]);
  const activeId = selected || items[0]?.task_id || "";
  const detail = useQuery({
    queryKey: ["audit-detail", activeId],
    queryFn: () => api.audit(activeId),
    enabled: Boolean(activeId),
  });
  const decide = useMutation({
    mutationFn: (body: { verdict: "pass" | "reject"; reason?: string; disposition?: "redo_feedback" | "manual" | "void" }) =>
      api.decideAudit(activeId, body),
    onSuccess: (record) => {
      toast(record.verdict === "pass" ? "抽检已通过，确认记录仍保留原 ackedBy" : "已打回并记录处置方式", "ok");
      setRejectOpen(false);
      setReason("");
      void queryClient.invalidateQueries();
      const remaining = items.filter((item) => item.task_id !== activeId);
      setParams(remaining[0] ? { task: remaining[0].task_id } : {}, { replace: true });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const index = items.findIndex((item) => item.task_id === activeId);
  const move = (delta: number) => {
    if (!items.length) return;
    const next = (Math.max(0, index) + delta + items.length) % items.length;
    setParams({ task: items[next].task_id });
  };

  useEffect(() => {
    if (!selected && items[0]) setParams({ task: items[0].task_id }, { replace: true });
  }, [items, selected, setParams]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, select, button, a") || document.querySelector(".modal-layer")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "a") decide.mutate({ verdict: "pass" });
      if (event.key === "r") setRejectOpen(true);
      if (event.key === "]") move(1);
      if (event.key === "[") move(-1);
      if (event.code === "Space" && video.current) {
        event.preventDefault();
        if (video.current.paused) void video.current.play();
        else video.current.pause();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <div className="page audit-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">HUMAN REVIEW / READ ONLY</span>
          <h1>抽检 <span className="page-count">{items.length}</span></h1>
        </div>
        <div className="page-actions">
          <Link className="button button-secondary button-small" to="/export">去导出</Link>
        </div>
      </header>

      {queue.isError ? <ErrorPanel error={queue.error} onRetry={() => void queue.refetch()} /> : null}
      {queue.isPending ? <LoadingLines rows={8} /> : null}
      {!queue.isPending && !items.length ? (
        <EmptyState
          icon={<ClipboardCheck size={30} />}
          title="全部抽检完毕。现在可以生成提交批次。"
          action={<Link className="button button-primary button-default" to="/export">去导出</Link>}
        />
      ) : items.length ? (
        <div className="audit-layout">
          <aside className="audit-queue">
            <header><strong>待抽检</strong><span>{items.length}</span></header>
            {items.map((item) => (
              <button
                type="button"
                key={item.task_id}
                className={activeId === item.task_id ? "is-active" : ""}
                onClick={() => setParams({ task: item.task_id })}
              >
                <span>{item.task_name}</span>
                <small>{item.task_type || "未分类"} · 自动确认</small>
              </button>
            ))}
            <div className="audit-shortcuts">
              <span><kbd>a</kbd> 通过</span>
              <span><kbd>r</kbd> 打回</span>
              <span><kbd>[</kbd><kbd>]</kbd> 切换</span>
              <span><kbd>Space</kbd> 播放</span>
            </div>
          </aside>

          <main className="audit-review">
            {detail.isPending && activeId ? <LoadingLines rows={10} /> : null}
            {detail.isError ? <ErrorPanel error={detail.error} onRetry={() => void detail.refetch()} /> : null}
            {detail.data ? <AuditContent detail={detail.data} videoRef={video} /> : null}
          </main>

          <aside className="audit-actions">
            <div className="audit-verdict">
              <span>当前 {index >= 0 ? index + 1 : 0} / {items.length}</span>
              <Button variant="primary" data-primary onClick={() => decide.mutate({ verdict: "pass" })} loading={decide.isPending}>
                <Check size={15} /> 通过 <kbd>a</kbd>
              </Button>
              <Button variant="danger" disabled={!detail.data} onClick={() => setRejectOpen(true)}>
                <X size={15} /> 打回 <kbd>r</kbd>
              </Button>
              <Button onClick={() => move(-1)} disabled={items.length < 2}><RotateCcw size={14} /> 上一题</Button>
              <Button onClick={() => move(1)} disabled={items.length < 2}>下一题</Button>
              {detail.data ? <Link className="button button-ghost button-default" to={`/tasks/${detail.data.task.id}`}>打开任务详情</Link> : null}
            </div>
            <Panel title="最近 Feedback 开头">
              {detail.data?.recent_feedback.length ? detail.data.recent_feedback.map((item) => (
                <div className="recent-feedback" key={item.task_id}>
                  <strong>{item.task_name}</strong>
                  <p>{item.head}</p>
                </div>
              )) : <p className="empty-inline">没有可对比的历史 Feedback。</p>}
            </Panel>
            <div className="readonly-seal"><ShieldAlert size={14} /> 本页只读。平台不会补全、润色、改写 Feedback，也不会修改分数。</div>
          </aside>
        </div>
      ) : null}

      <Modal
        open={rejectOpen}
        title="打回抽检"
        onClose={() => setRejectOpen(false)}
        footer={
          <>
            <Button onClick={() => setRejectOpen(false)}>取消</Button>
            <Button
              variant="danger"
              disabled={!reason.trim()}
              loading={decide.isPending}
              onClick={() => decide.mutate({ verdict: "reject", reason: reason.trim(), disposition })}
            >
              确认打回
            </Button>
          </>
        }
      >
        <Field label="打回原因" hint="必填；原因原文会出现在历程与收件箱中">
          <input value={reason} onChange={(event) => setReason(event.target.value)} autoFocus />
        </Field>
        <Field label="处置方式">
          <select value={disposition} onChange={(event) => setDisposition(event.target.value as typeof disposition)}>
            <option value="redo_feedback">交回自动交付重写 Feedback</option>
            <option value="manual">转人工处理</option>
            <option value="void">作废本题</option>
          </select>
        </Field>
        <p className="form-note">
          「重写 Feedback」只会撤销平台的自动确认记录并回到交付阶段；旧稿由 Agent 按提示词覆盖，平台不直接改写正文。
        </p>
      </Modal>
    </div>
  );
}

function AuditContent({ detail, videoRef }: { detail: AuditDetail; videoRef: RefObject<HTMLVideoElement | null> }) {
  const failedSteps = detail.timeline.steps.filter((step) => step.status === "failed");
  return (
    <>
      <section className="review-section">
        <header><strong>题面</strong><span className="mono">{detail.prompt.chars} 字 · 只读</span></header>
        <pre className="review-copy">{detail.prompt.installed || detail.prompt.draft || "题面文件不存在"}</pre>
      </section>
      <section className="review-section">
        <header><strong>轨迹摘要</strong><span className="mono">{detail.timeline.counts.total} 步 · 失败 {detail.timeline.counts.failed}</span></header>
        {failedSteps.length ? (
          <ol className="failed-steps">
            {failedSteps.slice(0, 8).map((step) => <li key={step.no}><span>第 {step.no} 步</span><code>{step.summary}</code></li>)}
          </ol>
        ) : <p className="empty-inline">没有失败的工具步骤。</p>}
      </section>
      <section className="review-section">
        <header><strong>产物 diff</strong><span className="mono">+{detail.diff.total.add} −{detail.diff.total.del} · {detail.diff.total.files} 文件</span></header>
        <div className="diff-mini-list">
          {detail.diff.files.slice(0, 12).map((file) => (
            <div key={file.path}><code>{file.path}</code><span>+{file.add} −{file.del}</span></div>
          ))}
          {!detail.diff.files.length ? <p className="empty-inline">没有可展示的 diff。</p> : null}
        </div>
      </section>
      <section className="review-section">
        <header><strong>Feedback</strong><Badge tone={detail.feedback.checked.ok ? "ok" : "danger"}>{detail.feedback.checked.score ?? "—"} 分</Badge></header>
        <pre className="review-copy feedback-copy">{detail.feedback.text || "Feedback 不存在"}</pre>
        <div className="quality-facts">
          <span>{detail.feedback.chars} 字</span>
          <span>{detail.feedback.anchors} 个锚点</span>
          <span>过程 {detail.feedback.coverage.process ? "已覆盖" : "未覆盖"}</span>
          <span>产物 {detail.feedback.coverage.artifact ? "已覆盖" : "未覆盖"}</span>
        </div>
      </section>
      <section className="review-section">
        <header><strong>录屏与门禁</strong><StatusDot state={detail.recording.ok ? "done" : "blocked"} label={detail.recording.ok ? "证据齐全" : "证据未通过"} /></header>
        <video ref={videoRef} controls preload="metadata" src={`/api/tasks/${encodeURIComponent(detail.task.id)}/recording/video`} />
        <div className="gate-grid">
          {detail.steps.map((step) => (
            <div key={step.number} className={step.ok ? "is-ok" : "is-bad"}>
              <span>{step.ok ? "✓" : "×"}</span>
              <strong>{step.number}. {step.name}</strong>
              <small>{step.detail || (step.ok ? "通过" : "未通过")}</small>
            </div>
          ))}
        </div>
      </section>
      <section className="review-section">
        <header><strong>历程</strong><span className="mono">确认人 {detail.task.ack_by || "未知"}</span></header>
        <p className="audit-id">任务创建 {formatDateTime(detail.task.created_at)} · SessionID <code>{detail.task.session_id || "—"}</code></p>
      </section>
    </>
  );
}
