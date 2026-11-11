import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { errorText, statusLabel } from "../utils/display";
import { useToast } from "./toast-context";
import { Button, ErrorPanel, LoadingLines, Modal } from "./ui";

function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** 清理任务：先列出会动哪些目录，确认后默认移入回收站，可勾选永久删除。 */
export function PurgeModal({
  taskIds,
  open,
  onClose,
  onDone,
}: {
  taskIds: string[];
  open: boolean;
  onClose: () => void;
  /** 参数是确实被清理掉的任务 id */
  onDone: (removed: string[]) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [permanent, setPermanent] = useState(false);
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (open) setSettled(false);
  }, [open]);
  const preview = useQuery({
    queryKey: ["purge-preview", taskIds],
    queryFn: () => api.purgeTasks(taskIds, { dryRun: true }),
    // 一旦开始清理就不再刷新预览：任务已经不在了，全局刷新再查只会得到 404
    enabled: open && taskIds.length > 0 && !settled,
    gcTime: 0,
  });
  const items = preview.data?.items ?? [];
  const allowed = items.filter((item) => !item.blockers.length);
  const purge = useMutation({
    onMutate: () => setSettled(true),
    mutationFn: () => api.purgeTasks(allowed.map((item) => item.task_id), { dryRun: false, permanent }),
    onSuccess: (result) => {
      const removed = result.items.filter((item) => item.removed);
      const failed = result.items.filter((item) => !item.removed);
      const releaseFailed = removed.filter((item) => item.release_error).length;
      const moved = removed.filter((item) => item.moved_to && !permanent);
      const undo = moved.length
        ? {
            label: "撤销",
            onClick: async () => {
              const results = await Promise.allSettled(
                moved.map((item) => api.restoreTrash(item.moved_to!.split("/").pop() ?? "")),
              );
              const failed = results.filter((result) => result.status === "rejected");
              toast(
                failed.length
                  ? `撤销完成 ${results.length - failed.length} 题，${failed.length} 题失败：${errorText(failed[0]?.reason)}`
                  : `已撤销清理，恢复 ${results.length} 题`,
                failed.length ? "warn" : "ok",
              );
              void queryClient.invalidateQueries();
            },
          }
        : undefined;
      toast(
        `已清理 ${removed.length} 题${permanent ? "（永久删除）" : "，已移入回收站"}` +
          (failed.length ? `；${failed.length} 题失败：${failed[0].error ?? failed[0].blockers[0] ?? ""}` : "") +
          (releaseFailed ? `；${releaseFailed} 题工程台归还未成功` : ""),
        failed.length || releaseFailed ? "warn" : "ok",
        undo,
      );
      setPermanent(false);
      const gone = removed.map((item) => item.task_id);
      onDone(gone);
      // 只刷新列表类数据；被清理任务自己的查询不再重查
      void queryClient.invalidateQueries({
        predicate: (query) => !query.queryKey.some((part) => typeof part === "string" && gone.includes(part)),
      });
    },
    onError: (error) => {
      setSettled(false);
      toast(errorText(error), "error");
    },
  });

  return (
    <Modal
      open={open}
      title={`清理 ${taskIds.length} 个任务`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="danger" disabled={!allowed.length} loading={purge.isPending} onClick={() => purge.mutate()}>
            {permanent ? "永久删除" : "移入回收站"}
            {allowed.length ? ` · ${allowed.length} 题` : ""}
          </Button>
        </>
      }
    >
      {preview.isPending ? <LoadingLines rows={3} /> : null}
      {preview.isError ? <ErrorPanel error={preview.error} onRetry={() => void preview.refetch()} /> : null}
      {preview.data ? (
        <div className="confirm-copy">
          <p>
            会清掉这些任务的容器，把任务从平台移除，并处理下面列出的本地目录
            {allowed.length ? `（共 ${formatSize(preview.data.total_bytes)}）` : ""}。
          </p>
          <ul className="purge-list">
            {items.map((item) => (
              <li key={item.task_id} className={item.blockers.length ? "is-blocked" : undefined}>
                <div className="purge-head">
                  <strong className="ellipsis" title={item.task_name}>{item.task_name}</strong>
                  <span>{statusLabel(item.job_status)} · {formatSize(item.size)}</span>
                </div>
                {item.blockers.map((text) => (
                  <p key={text} className="danger-text">不能清理：{text}</p>
                ))}
                {item.blockers.length ? null : item.warnings.map((text) => <p key={text}>{text}</p>)}
                {item.blockers.length ? null : item.paths.map((path) => (
                  <code key={path} title={path}>{path}</code>
                ))}
              </li>
            ))}
          </ul>
          <label className="purge-option">
            <input type="checkbox" checked={permanent} onChange={(event) => setPermanent(event.target.checked)} />
            <span>
              永久删除，不进回收站
              <small>
                {permanent
                  ? "删除后无法恢复。"
                  : `默认移到 ${preview.data.trash}；10 秒内可撤销，之后仍可在回收站恢复。`}
              </small>
            </span>
          </label>
        </div>
      ) : null}
    </Modal>
  );
}
