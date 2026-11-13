import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Copy, Download, Search, Upload, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import type { PromptLibraryItem } from "../../api/types";
import { errorText } from "../../utils/display";
import { formatDateTime } from "../../utils/time";
import { useToast } from "../toast-context";
import { Badge, Button, EmptyState, ErrorPanel, LoadingLines, Panel } from "../ui";

const GROUP_PAGE_SIZE = 10;
const EMPTY_ITEMS: PromptLibraryItem[] = [];
const SOURCE_LABEL: Record<string, string> = {
  live: "任务",
  history: "历史",
  import: "导入",
};

type PromptEngineeringGroup = {
  key: string;
  engineeringId: string;
  name: string;
  latestAt: string;
  items: PromptLibraryItem[];
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
  const initializedProject = useRef("");
  const [search, setSearch] = useState("");
  const [groupLimit, setGroupLimit] = useState(GROUP_PAGE_SIZE);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const prompts = useQuery({
    queryKey: ["source-prompts", projectId],
    queryFn: () => api.promptLibrary(projectId),
    enabled: Boolean(projectId),
  });

  const allItems = prompts.data?.items ?? EMPTY_ITEMS;
  const allGroups = useMemo(() => groupPrompts(allItems), [allItems]);
  const matchingItems = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return allItems;
    return allItems.filter((item) =>
      [
        item.text,
        item.engineeringName,
        item.taskName,
        item.subDirection,
        item.taskType,
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [allItems, search]);
  const groups = useMemo(() => groupPrompts(matchingItems), [matchingItems]);
  const visibleGroups = groups.slice(0, groupLimit);
  const importedCount = allItems.filter((item) => item.source === "import").length;

  useEffect(() => {
    setSearch("");
    setGroupLimit(GROUP_PAGE_SIZE);
    setExpandedGroups(new Set());
  }, [projectId]);

  useEffect(() => {
    if (!prompts.data || initializedProject.current === projectId) return;
    initializedProject.current = projectId;
    const latest = allGroups[0];
    if (latest) setExpandedGroups(new Set([latest.key]));
  }, [allGroups, projectId, prompts.data]);

  useEffect(() => {
    if (search.trim() && groups.length) {
      setExpandedGroups(new Set(groups.map((group) => group.key)));
    }
  }, [groups, search]);

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

  function toggleGroup(key: string): void {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function setAllGroups(open: boolean): void {
    setExpandedGroups(open ? new Set(groups.map((group) => group.key)) : new Set());
  }

  if (!projectId) return null;

  const allExpanded = groups.length > 0 && groups.every((group) => expandedGroups.has(group.key));

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
      <div className="prompt-library-overview">
        <div>
          <p>按工程归档。展开一个工程，查看它实际使用过的全部题面。</p>
          <div className="prompt-library-stats" aria-label="提示词库概况">
            <span>
              <strong>{allItems.length}</strong> 条提示词
            </span>
            <span>
              <strong>{allGroups.length}</strong> 个工程
            </span>
            <span>
              <strong>{importedCount}</strong> 条导入
            </span>
          </div>
        </div>
        <span className="prompt-library-project">{projectName || "当前项目"}</span>
      </div>

      <div className="prompt-library-toolbar">
        <label className="search-box prompt-library-search">
          <Search size={14} />
          <input
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setGroupLimit(GROUP_PAGE_SIZE);
            }}
            placeholder="搜索工程、任务或提示词"
          />
          {search ? (
            <button
              type="button"
              aria-label="清除提示词搜索"
              onClick={() => {
                setSearch("");
                setGroupLimit(GROUP_PAGE_SIZE);
              }}
            >
              <X size={13} />
            </button>
          ) : null}
        </label>
        <div className="prompt-library-view-actions">
          <span>
            {search ? `匹配 ${groups.length} 个工程` : `显示 ${visibleGroups.length}/${groups.length} 个工程`}
          </span>
          {groups.length ? (
            <Button
              size="small"
              variant="ghost"
              onClick={() => setAllGroups(!allExpanded)}
            >
              {allExpanded ? "全部收起" : "全部展开"}
            </Button>
          ) : null}
        </div>
      </div>

      {prompts.isPending ? <LoadingLines rows={5} /> : null}
      {prompts.isError ? (
        <ErrorPanel error={prompts.error} onRetry={() => void prompts.refetch()} />
      ) : null}
      {!prompts.isPending && !prompts.isError && !groups.length ? (
        <EmptyState
          title={
            search
              ? "没有匹配的工程或提示词。"
              : "这个项目还没有用过的提示词，可导入已有 JSON/JSONL。未关联工程的记录会单独归组。"
          }
        />
      ) : null}

      <div className="prompt-engineering-list">
        {visibleGroups.map((group) => (
          <PromptEngineeringSection
            key={group.key}
            group={group}
            expanded={expandedGroups.has(group.key)}
            onToggle={() => toggleGroup(group.key)}
            onCopy={(item) => {
              void navigator.clipboard
                .writeText(item.text)
                .then(() => toast("提示词已复制", "ok"))
                .catch((error: unknown) => toast(errorText(error), "error"));
            }}
          />
        ))}
      </div>

      {visibleGroups.length < groups.length ? (
        <div className="prompt-library-more">
          <Button
            size="small"
            variant="ghost"
            onClick={() => setGroupLimit((value) => value + GROUP_PAGE_SIZE)}
          >
            再显示 {Math.min(GROUP_PAGE_SIZE, groups.length - visibleGroups.length)} 个工程
          </Button>
        </div>
      ) : null}
    </Panel>
  );
}

function PromptEngineeringSection({
  group,
  expanded,
  onToggle,
  onCopy,
}: {
  group: PromptEngineeringGroup;
  expanded: boolean;
  onToggle: () => void;
  onCopy: (item: PromptLibraryItem) => void;
}) {
  const panelId = `prompt-group-${group.key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  return (
    <section className={`prompt-engineering-group ${expanded ? "is-open" : ""}`}>
      <button
        type="button"
        className="prompt-engineering-head"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <span className="prompt-engineering-caret" aria-hidden="true">
          <ChevronRight size={16} />
        </span>
        <span className="prompt-engineering-name">
          <strong>{group.name}</strong>
          <small>{group.items.length} 条提示词</small>
        </span>
        <span className="prompt-engineering-time">
          <small>最近使用</small>
          <time dateTime={group.latestAt || undefined}>
            {group.latestAt ? formatDateTime(group.latestAt) : "时间未知"}
          </time>
        </span>
      </button>
      {expanded ? (
        <div id={panelId} className="prompt-engineering-body">
          {group.items.map((item, index) => {
            const recordTitle =
              item.taskName && item.taskName !== group.name
                ? item.taskName
                : item.source === "import"
                  ? "手动导入"
                  : "工程题面";
            return (
              <article key={item.recordId} className="prompt-record">
                <span className="prompt-record-index" aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div className="prompt-record-main">
                  <header>
                    <strong>{recordTitle}</strong>
                    <span className={`prompt-record-source is-${item.source}`}>
                      {SOURCE_LABEL[item.source] ?? item.source}
                    </span>
                    <time dateTime={item.usedAt || undefined}>
                      {item.usedAt ? formatDateTime(item.usedAt) : "时间未知"}
                    </time>
                  </header>
                  <p>{item.text}</p>
                  <footer>
                    <span>
                      {[item.subDirection, item.taskType].filter(Boolean).join(" · ") ||
                        "未标注分类"}
                    </span>
                    <Button size="small" variant="ghost" onClick={() => onCopy(item)}>
                      <Copy size={12} /> 复制
                    </Button>
                  </footer>
                </div>
              </article>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function groupPrompts(items: PromptLibraryItem[]): PromptEngineeringGroup[] {
  const byEngineering = new Map<string, PromptEngineeringGroup>();
  for (const item of items) {
    const key = item.engineeringId || "__unassigned__";
    let group = byEngineering.get(key);
    if (!group) {
      group = {
        key,
        engineeringId: item.engineeringId,
        name: item.engineeringName || item.taskName || "未关联工程",
        latestAt: item.usedAt,
        items: [],
      };
      byEngineering.set(key, group);
    }
    group.items.push(item);
    if ((item.usedAt || "") > group.latestAt) group.latestAt = item.usedAt;
  }
  return [...byEngineering.values()]
    .map((group) => ({
      ...group,
      items: group.items.sort((left, right) => (right.usedAt || "").localeCompare(left.usedAt || "")),
    }))
    .sort((left, right) => (right.latestAt || "").localeCompare(left.latestAt || ""));
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
