import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, Search, Upload } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import type { PromptLibraryItem } from "../../api/types";
import { useToast } from "../toast-context";
import { Badge, Button, EmptyState, ErrorPanel, LoadingLines, Panel } from "../ui";
import { errorText } from "../../utils/display";
import { formatDateTime } from "../../utils/time";

const PAGE_SIZE = 24;
const SOURCE_LABEL: Record<string, string> = {
  live: "任务",
  history: "历史",
  import: "导入",
};

export function PromptLibraryPanel({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const prompts = useQuery({
    queryKey: ["source-prompts", projectId],
    queryFn: () => api.promptLibrary(projectId),
    enabled: Boolean(projectId),
  });
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const items = prompts.data?.items ?? [];
    if (!needle) return items;
    return items.filter((item) =>
      [item.text, item.taskName, item.subDirection, item.taskType]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [prompts.data?.items, search]);
  const visible = filtered.slice(0, limit);

  const importFile = useMutation({
    mutationFn: async (file: File) => {
      const items = parsePromptFile(await readFileText(file));
      return api.importPromptLibrary(projectId, items, file.name);
    },
    onSuccess: (result) => {
      toast(
        `导入 ${result.imported} 条，跳过重复或无效 ${result.skipped} 条`,
        result.errors.length ? "warn" : "ok",
      );
      if (fileRef.current) fileRef.current.value = "";
      void queryClient.invalidateQueries({ queryKey: ["source-prompts", projectId] });
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  const exportFile = useMutation({
    mutationFn: () => api.exportPromptLibrary(projectId),
    onSuccess: (text) => {
      const stamp = new Date().toISOString().slice(0, 10).replaceAll("-", "");
      downloadText(
        text,
        `prompt-library-${safeFilename(projectName || projectId)}-${stamp}.json`,
      );
      toast("提示词库已导出", "ok");
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  if (!projectId) return null;

  return (
    <Panel
      className="prompt-library-panel"
      title={
        <span className="prompt-library-title">
          项目提示词库
          <Badge tone="info">{prompts.data?.total ?? 0}</Badge>
        </span>
      }
      actions={
        <div className="row-actions">
          <input
            ref={fileRef}
            type="file"
            accept=".json,.jsonl,application/json"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) importFile.mutate(file);
            }}
          />
          <Button
            size="small"
            variant="ghost"
            loading={importFile.isPending}
            onClick={() => fileRef.current?.click()}
          >
            <Upload size={13} /> 导入
          </Button>
          <Button
            size="small"
            variant="ghost"
            loading={exportFile.isPending}
            disabled={!prompts.data?.total}
            onClick={() => exportFile.mutate()}
          >
            <Download size={13} /> 导出
          </Button>
        </div>
      }
    >
      <div className="prompt-library-toolbar">
        <label className="search-box">
          <Search size={14} />
          <input
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setLimit(PAGE_SIZE);
            }}
            placeholder="搜索题面、任务名、子方向"
          />
        </label>
        <span>
          {projectName || "当前项目"} · 显示 {visible.length}/{filtered.length}
          {filtered.length !== (prompts.data?.items.length ?? 0)
            ? ` · 共 ${prompts.data?.items.length ?? 0}`
            : ""}
        </span>
      </div>

      {prompts.isPending ? <LoadingLines rows={4} /> : null}
      {prompts.isError ? (
        <ErrorPanel error={prompts.error} onRetry={() => void prompts.refetch()} />
      ) : null}
      {!prompts.isPending && !prompts.isError && !filtered.length ? (
        <EmptyState
          title={search ? "没有匹配的提示词。" : "这个项目还没有用过的提示词，可导入已有 JSON/JSONL。"}
        />
      ) : null}

      <div className="prompt-library-grid">
        {visible.map((item) => (
          <PromptCard
            key={item.recordId}
            item={item}
            onCopy={() => {
              void navigator.clipboard
                .writeText(item.text)
                .then(() => toast("提示词已复制", "ok"))
                .catch((error: unknown) => toast(errorText(error), "error"));
            }}
          />
        ))}
      </div>

      {visible.length < filtered.length ? (
        <div className="prompt-library-more">
          <Button size="small" variant="ghost" onClick={() => setLimit((value) => value + PAGE_SIZE)}>
            再显示 {Math.min(PAGE_SIZE, filtered.length - visible.length)} 条
          </Button>
        </div>
      ) : null}
    </Panel>
  );
}

function PromptCard({ item, onCopy }: { item: PromptLibraryItem; onCopy: () => void }) {
  return (
    <article className="prompt-library-card">
      <header>
        <div>
          <strong>{item.taskName || "导入的提示词"}</strong>
          <span>
            {[item.subDirection, item.taskType].filter(Boolean).join(" · ") || "未标注分类"}
          </span>
        </div>
        <Badge tone={item.source === "import" ? "warn" : "idle"}>
          {SOURCE_LABEL[item.source] ?? item.source}
        </Badge>
      </header>
      <p>{item.text}</p>
      <footer>
        <time dateTime={item.usedAt || undefined}>
          {item.usedAt ? formatDateTime(item.usedAt) : "时间未知"}
        </time>
        <Button size="small" variant="ghost" onClick={onCopy}>
          <Copy size={12} /> 复制
        </Button>
      </footer>
    </article>
  );
}

function parsePromptFile(text: string): Array<Record<string, unknown>> {
  const trimmed = text.trim();
  if (!trimmed) throw new Error("导入文件为空");
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    const lines = trimmed.split(/\r?\n/).filter((line) => line.trim());
    parsed = lines.map((line, index) => {
      try {
        return JSON.parse(line) as unknown;
      } catch {
        throw new Error(`JSONL 第 ${index + 1} 行格式错误`);
      }
    });
  }
  let rawItems: unknown[];
  if (Array.isArray(parsed)) {
    rawItems = parsed;
  } else if (parsed && typeof parsed === "object" && "items" in parsed) {
    if (!Array.isArray((parsed as { items?: unknown }).items)) {
      throw new Error("导出文件的 items 字段应为数组");
    }
    rawItems = (parsed as { items: unknown[] }).items;
  } else if (parsed && typeof parsed === "object") {
    // A JSONL file with one entry parses as a single prompt object.
    rawItems = [parsed];
  } else {
    throw new Error("文件应为 JSON 数组、JSONL，或包含 items 数组的导出文件");
  }
  const items = rawItems.map((item, index) => {
    if (typeof item === "string") return { text: item };
    if (!item || typeof item !== "object") throw new Error(`第 ${index + 1} 条不是提示词对象`);
    return item as Record<string, unknown>;
  });
  if (!items.length) throw new Error("导入文件没有提示词");
  if (items.length > 1000) throw new Error("单次最多导入 1000 条提示词");
  return items;
}

function safeFilename(value: string): string {
  return value.replace(/[\\/:*?"<>|\s]+/g, "-").slice(0, 60) || "project";
}

function downloadText(text: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      resolve(
        typeof result === "string" ? result : result ? new TextDecoder().decode(result) : "",
      );
    };
    reader.onerror = () => reject(reader.error ?? new Error("导入文件读取失败"));
    reader.readAsText(file, "utf-8");
  });
}
