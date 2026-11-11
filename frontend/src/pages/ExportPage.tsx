import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Copy, Download, FileCheck2, FileSpreadsheet, FolderOpen, History, PackageCheck, RefreshCw, ShieldCheck, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { BatchCheck, BatchRow, ExportResult } from "../api/types";
import {
  Button,
  EmptyState,
  ErrorPanel,
  LoadingLines,
  Panel,
} from "../components/ui";
import { useToast } from "../components/toast-context";
import { errorText } from "../utils/display";
import { formatDateTime, todayShanghai } from "../utils/time";

export function ExportPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [date, setDate] = useState(todayShanghai());
  const [previewName, setPreviewName] = useState("");
  const [lastResult, setLastResult] = useState<ExportResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [includeExported, setIncludeExported] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [check, setCheck] = useState<BatchCheck | null>(null);
  const [createdBatch, setCreatedBatch] = useState<BatchRow | null>(null);
  const exports = useQuery({ queryKey: ["exports"], queryFn: api.exports });
  const tasks = useQuery({ queryKey: ["tasks", "batch-candidates"], queryFn: () => api.tasks({ page_size: 200 }), refetchInterval: 5000 });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.batches, refetchInterval: 5000 });
  const quality = useQuery({ queryKey: ["quality"], queryFn: () => api.quality(), refetchInterval: 30_000 });
  const preview = useQuery({
    queryKey: ["export-preview", previewName],
    queryFn: () => api.exportPreview(previewName),
    enabled: Boolean(previewName),
  });
  const create = useMutation({
    mutationFn: () => api.export({ date }),
    onSuccess: (result) => {
      setLastResult(result);
      if (result.file) {
        toast(
          result.skipped.length ? `导出了 ${result.rows} 行，另有 ${result.skipped.length} 题被跳过` : `导出了 ${result.rows} 行`,
          result.skipped.length ? "warn" : "ok",
        );
        setPreviewName(result.file.split("/").pop() ?? "");
      } else {
        // 0 行不是成功：没有文件生成，原因在下方逐条列出
        toast(
          result.skipped.length ? `没有生成表格：${result.skipped.length} 题未通过导出检查` : `${date} 没有可导出的任务`,
          "warn",
        );
      }
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const stats = useQuery({ queryKey: ["stats", date], queryFn: () => api.stats(date, date) });
  const reveal = useMutation({
    mutationFn: (name: string) => api.revealExport(name),
    onError: (error) => toast(errorText(error), "error"),
  });
  const candidates = useMemo(() => {
    const rows = tasks.data?.items ?? [];
    return rows.filter((task) => {
      const complete = (task.steps ?? []).filter((step) => step.state === "done").length >= 10;
      if (!complete) return false;
      if (!includeExported && (task.exported_at || (batches.data?.items ?? []).some((batch) => batch.task_ids.includes(task.id)))) {
        return false;
      }
      return true;
    });
  }, [batches.data?.items, includeExported, tasks.data?.items]);
  useEffect(() => {
    if (!selected.size && candidates.length) setSelected(new Set(candidates.map((task) => task.id)));
  }, [candidates, selected.size]);
  const runCheck = useMutation({
    mutationFn: () => api.batchCheck([...selected], includeExported),
    onSuccess: (result) => {
      setCheck(result);
      setAcknowledged(false);
      toast(result.ok ? "批次体检通过" : `发现 ${result.blockers.length} 个阻断项`, result.ok ? "ok" : "warn");
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const createBatch = useMutation({
    mutationFn: () => api.createBatch([...selected], includeExported, acknowledged),
    onSuccess: (batch) => {
      setCreatedBatch(batch);
      setCheck(batch.check);
      toast(`提交批次已生成：${batch.rows ?? batch.task_ids.length} 题`, "ok");
      setSelected(new Set());
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const bundle = useMutation({
    mutationFn: (id: string) => api.bundleBatch(id),
    onSuccess: (result) => toast(`提交包已生成：${result.bundle}`, "ok"),
    onError: (error) => toast(errorText(error), "error"),
  });
  const privacy = useMutation({
    mutationFn: () => api.privacyRecheck([...selected]),
    onSuccess: (result) => {
      toast(
        `隐私检查：通过 ${result.passed}，未检查 ${result.unchecked}，失败 ${result.failed}`,
        result.failed || result.unchecked ? "warn" : "ok",
      );
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  return (
    <div className="page export-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">DELIVERY / 19 COLUMNS</span>
          <h1>导出与统计</h1>
        </div>
        <div className="page-actions">
          <Link className="button button-secondary button-default" to="/usage"><History size={14} /> 使用统计</Link>
          <Button onClick={() => void exports.refetch()} loading={exports.isFetching && !exports.isPending}>
            <RefreshCw size={14} /> 刷新
          </Button>
          <Button variant="primary" data-primary disabled={!date} onClick={() => create.mutate()} loading={create.isPending}>
            <Upload size={14} /> 生成当日表
          </Button>
        </div>
      </header>

      <Panel className="batch-builder" title="生成提交批次">
        <div className="batch-toolbar">
          <label className="check-inline">
            <input type="checkbox" checked={includeExported} onChange={(event) => {
              setIncludeExported(event.target.checked);
              setCheck(null);
            }} />
            包含已导出/已进批次（标记重复导出）
          </label>
          <span className="toolbar-count">{selected.size} / {candidates.length} 题</span>
          <Button size="small" onClick={() => runCheck.mutate()} loading={runCheck.isPending} disabled={!selected.size}>
            <ShieldCheck size={13} /> 开始体检
          </Button>
          <Button size="small" onClick={() => privacy.mutate()} loading={privacy.isPending} disabled={!selected.size}>
            录屏隐私回查
          </Button>
          <Button
            size="small"
            variant="primary"
            data-primary
            onClick={() => createBatch.mutate()}
            loading={createBatch.isPending}
            disabled={!check?.ok || (Boolean(check.warnings.length) && !acknowledged)}
          >
            <PackageCheck size={13} /> 生成批次
          </Button>
        </div>
        {candidates.length ? (
          <div className="batch-candidates">
            {candidates.map((task) => (
              <label key={task.id}>
                <input
                  type="checkbox"
                  checked={selected.has(task.id)}
                  onChange={(event) => {
                    setSelected((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(task.id);
                      else next.delete(task.id);
                      return next;
                    });
                    setCheck(null);
                  }}
                />
                <span><strong>{task.task_name}</strong><small>{task.task_type || "未分类"} · {task.ack_by === "user" ? "人工确认" : "自动确认"}</small></span>
                {task.exported_at ? <FileSpreadsheet size={13} /> : null}
              </label>
            ))}
          </div>
        ) : <p className="empty-inline">没有满足基础完成条件的候选。可勾选「包含已导出」查看历史题。</p>}
        {check ? (
          <div className={`batch-report ${check.ok ? "is-ok" : "is-bad"}`}>
            <div className="batch-report-head">
              {check.ok ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
              <strong>{check.ok ? "体检通过" : `体检阻断 · ${check.blockers.length}`}</strong>
              <span>未经人工复核 {check.unaudited_count} 条</span>
            </div>
            {check.blockers.map((item) => <p key={`${item.code}-${item.task_id}-${item.message}`} className="danger-text"><strong>{item.code}</strong> {item.task}：{item.message}</p>)}
            {check.warnings.map((item) => <p key={`${item.code}-${item.task_id}-${item.message}`}><strong>{item.code}</strong> {item.task || ""} {item.message}</p>)}
            {check.warnings.length ? (
              <label className="check-inline">
                <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
                我已知晓全部提示项，仍要生成批次
              </label>
            ) : null}
          </div>
        ) : null}
        {createdBatch ? (
          <div className="batch-result">
            <FileCheck2 size={16} />
            <span>批次 {createdBatch.id} 已生成，成员 {createdBatch.task_ids.length} 题</span>
            <a className="button button-secondary button-small" href={`/api/batches/${createdBatch.id}/download`}><Download size={13} /> 下载 xlsx</a>
            <Button size="small" onClick={() => bundle.mutate(createdBatch.id)} loading={bundle.isPending}>生成提交包</Button>
          </div>
        ) : null}
      </Panel>

      <div className="export-layout">
        <Panel className="export-controls" title="生成参数">
          <label className="field">
            <span className="field-label">导出日期</span>
            <input type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          </label>
          <div className="metric-strip compact">
            <div className="metric"><strong>{stats.data?.summary.created ?? 0}</strong><span>创建题数</span></div>
            <div className="metric"><strong>{stats.data?.summary.exported ?? 0}</strong><span>已导出</span></div>
            <div className="metric"><strong>{stats.data?.days[0]?.failed ?? 0}</strong><span>失败</span></div>
          </div>
          {stats.data?.summary.average_minutes ? (
            <div className="config-facts">平均完成耗时 {stats.data.summary.average_minutes} 分钟</div>
          ) : null}
          {stats.data?.failures.length ? (
            <div className="compact-failures">
              {stats.data.failures.slice(0, 4).map((failure) => (
                <div key={`${failure.action}-${failure.message}`}>
                  <span>{failure.action}</span>
                  <strong>{failure.count}</strong>
                </div>
              ))}
            </div>
          ) : null}
          <p className="form-note">
            只有 11 步与人工复核都通过的任务会进入交付表。任何缺项都会在跳过原因中逐条列出。
          </p>
        </Panel>

        <Panel className="export-history" title="历史导出">
          {exports.isPending ? <LoadingLines rows={4} /> : null}
          {exports.isError ? <ErrorPanel error={exports.error} onRetry={() => void exports.refetch()} /> : null}
          {exports.data?.items.length ? (
            <div className="export-list">
              {exports.data.items.map((item) => (
                <div key={item.name} className={`export-item ${previewName === item.name ? "is-active" : ""}`}>
                  <button
                    type="button"
                    aria-current={previewName === item.name || undefined}
                    title={item.name}
                    onClick={() => setPreviewName(item.name)}
                  >
                    <FileSpreadsheet size={16} />
                    <span>
                      <strong>{item.name}</strong>
                      <small>{formatDateTime(item.created_at)} · {Math.max(1, Math.round(item.size / 1024))} KB</small>
                    </span>
                  </button>
                  <a
                    className="icon-button"
                    href={`/api/exports/${encodeURIComponent(item.name)}/download`}
                    aria-label={`下载 ${item.name}`}
                    title="下载"
                  >
                    <Download size={14} />
                  </a>
                </div>
              ))}
            </div>
          ) : exports.isPending || exports.isError ? null : (
            <EmptyState title="还没有导出文件。" />
          )}
        </Panel>
      </div>

      {lastResult?.skipped.length ? (
        <Panel title={`本次跳过 · ${lastResult.skipped.length}（技能返回的原因原文）`}>
          <div className="skip-list" role="alert">
            {lastResult.skipped.map((item) => (
              <div key={item.task_id || item.task}>
                <strong>{item.task || item.task_id}</strong>
                <span>{item.reason.join("；") || "技能未给出原因"}</span>
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      <Panel title={`提交批次历史 · ${batches.data?.items.length ?? 0}`}>
        {batches.data?.items.length ? (
          <div className="batch-history">
            {batches.data.items.map((batch) => (
              <div key={batch.id}>
                <FileCheck2 size={15} />
                <span>
                  <strong>{batch.id}</strong>
                  <small>
                    {formatDateTime(batch.created_at)} · {batch.rows ?? batch.task_ids.length} 题 · 未复核 {batch.unaudited_count}
                  </small>
                </span>
                <div className="row-actions">
                  <a className="button button-secondary button-small" href={`/api/batches/${batch.id}/download`}>
                    <Download size={12} /> xlsx
                  </a>
                  <Button size="small" onClick={() => bundle.mutate(batch.id)} loading={bundle.isPending && bundle.variables === batch.id}>
                    提交包
                  </Button>
                </div>
              </div>
            ))}
          </div>
        ) : <p className="empty-inline">还没有提交批次。批次成员会写入使用台账的「提交次数」。</p>}
      </Panel>

      <Panel title="质量看板 · 只读">
        {quality.data ? (
          <div className="quality-board">
            {quality.data.warning ? <p className="quality-warning"><AlertTriangle size={14} /> {quality.data.warning}</p> : null}
            <div>
              <strong>Feedback 分数分布</strong>
              <ScoreBars values={quality.data.scores} />
            </div>
            <div>
              <strong>失败类别</strong>
              {quality.data.failures.length ? quality.data.failures.map((item) => (
                <div className="quality-row" key={item.kind}><span>{item.kind}</span><i style={{ width: `${Math.min(100, item.count * 12)}%` }} /><strong>{item.count}</strong></div>
              )) : <p className="empty-inline">暂无失败类别。</p>}
            </div>
            <div>
              <strong>抽检与确认</strong>
              <p>抽样 {quality.data.audit.sampled} · 已决 {quality.data.audit.decided} · 通过率 {Math.round(quality.data.audit.pass_rate * 100)}%</p>
              <p>自动确认 {quality.data.acknowledgement.autopilot ?? 0} · 人工确认 {quality.data.acknowledgement.user ?? 0}</p>
              <p>Feedback 长度 {quality.data.feedback_lengths.min}–{quality.data.feedback_lengths.max} 字，均值 {quality.data.feedback_lengths.average}</p>
            </div>
          </div>
        ) : <p className="empty-inline">质量统计加载中。</p>}
      </Panel>

      {previewName ? (
        <Panel title={`表格预览 · ${previewName}`} className="preview-panel">
          <div className="export-actions">
            <Button onClick={() => reveal.mutate(previewName)} loading={reveal.isPending}>
              <FolderOpen size={14} /> 在 Finder 中显示
            </Button>
            <a className="button button-secondary button-default" href={`/api/exports/${encodeURIComponent(previewName)}/download`}>
              <Download size={14} /> 下载
            </a>
            <Button
              onClick={() => {
                const path = exports.data?.items.find((item) => item.name === previewName)?.path;
                if (!path) return;
                navigator.clipboard.writeText(path).then(
                  () => toast("导出路径已复制", "info"),
                  (error: unknown) => toast(`复制失败：${errorText(error)}`, "error"),
                );
              }}
            >
              <Copy size={14} /> 复制路径
            </Button>
          </div>
          {preview.isPending ? <LoadingLines rows={6} /> : null}
          {preview.isError ? <ErrorPanel error={preview.error} onRetry={() => void preview.refetch()} /> : null}
          {preview.data ? (
            <div className="table-scroll">
              <table className="data-table preview-table">
                <thead>
                  <tr>
                    {preview.data.headers.map((header, index) => (
                      <th key={`${String(header)}-${index}`}>{String(header ?? "—")}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex} title={String(cell ?? "")}>{String(cell ?? "—")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

function ScoreBars({ values }: { values: Array<{ score: string; count: number }> }) {
  const max = Math.max(1, ...values.map((item) => item.count));
  return (
    <svg className="score-bars-svg" viewBox="0 0 260 112" role="img" aria-label="Feedback 分数分布">
      {[1, 2, 3, 4, 5].map((score) => {
        const count = Number(values.find((item) => Number(item.score) === score)?.count ?? 0);
        const y = 8 + (score - 1) * 21;
        const width = Math.max(0, (count / max) * 180);
        return (
          <g key={score}>
            <text x="0" y={y + 8} className="score-label">{score} 分</text>
            <rect x="42" y={y} width="180" height="9" rx="4" className="score-track" />
            <rect x="42" y={y} width={width} height="9" rx="4" className="score-value" />
            <text x="256" y={y + 8} textAnchor="end" className="score-count">{count}</text>
          </g>
        );
      })}
    </svg>
  );
}
