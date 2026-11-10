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
      toast(`导出了 ${result.rows} 行`, "ok");
      if (result.file) setPreviewName(result.file.split("/").pop() ?? "");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });
  const stats = useQuery({ queryKey: ["stats", date], queryFn: () => api.stats(date, date) });
  const reveal = useMutation({
    mutationFn: (name: string) => api.revealExport(name),
    onError: (error) => toast(error instanceof Error ? error.message : String(error), "error"),
  });

  return (
    <div className="page export-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">DELIVERY / 19 COLUMNS</span>
          <h1>导出与统计</h1>
        </div>
        <div className="page-actions">
          <Button onClick={() => void exports.refetch()}><RefreshCw size={14} /> 刷新</Button>
          <Button variant="primary" onClick={() => create.mutate()} loading={create.isPending}>
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
                <button
                  key={item.name}
                  className={previewName === item.name ? "is-active" : ""}
                  onClick={() => setPreviewName(item.name)}
                >
                  <FileSpreadsheet size={16} />
                  <span>
                    <strong>{item.name}</strong>
                    <small>{formatDateTime(item.created_at)} · {(item.size / 1024).toFixed(0)} KB</small>
                  </span>
                  <a
                    className="icon-button"
                    href={`/api/exports/${encodeURIComponent(item.name)}/download`}
                    aria-label="下载"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <Download size={14} />
                  </a>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState title="还没有导出文件。" />
          )}
        </Panel>
      </div>

      {lastResult?.skipped.length ? (
        <Panel title={`本次跳过 · ${lastResult.skipped.length}`}>
          <div className="skip-list">
            {lastResult.skipped.map((item) => (
              <div key={item.task_id || item.task}>
                <strong>{item.task}</strong>
                <span>{item.reason.join("；")}</span>
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
                if (path) void navigator.clipboard.writeText(path).then(() => toast("导出路径已复制", "info"));
              }}
            >
              <Copy size={14} /> 复制路径
            </Button>
          </div>
          {preview.isPending ? <LoadingLines rows={6} /> : null}
          {preview.isError ? <ErrorPanel error={preview.error} /> : null}
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
                        <td key={cellIndex}>{String(cell ?? "—")}</td>
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
