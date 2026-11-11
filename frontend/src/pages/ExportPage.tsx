import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, FileSpreadsheet, FolderOpen, RefreshCw, Upload } from "lucide-react";
import { useState } from "react";

import { api } from "../api/client";
import type { ExportResult } from "../api/types";
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
  const exports = useQuery({ queryKey: ["exports"], queryFn: api.exports });
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

  return (
    <div className="page export-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">DELIVERY / 19 COLUMNS</span>
          <h1>导出与统计</h1>
        </div>
        <div className="page-actions">
          <Button onClick={() => void exports.refetch()} loading={exports.isFetching && !exports.isPending}>
            <RefreshCw size={14} /> 刷新
          </Button>
          <Button variant="primary" data-primary disabled={!date} onClick={() => create.mutate()} loading={create.isPending}>
            <Upload size={14} /> 生成当日表
          </Button>
        </div>
      </header>

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
