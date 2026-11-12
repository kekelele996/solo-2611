import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { errorText } from "../utils/display";
import { formatDateTime } from "../utils/time";
import { useToast } from "./toast-context";
import { Button, Field, Modal, Panel } from "./ui";

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  paused: "已暂停",
  stopped: "已终止",
  completed: "已完成",
};

const CONTINUOUS_DAILY_CAP = 30;

/** 生产计划：查看进度、暂停/继续/终止、转为持续生产或下达新计划。 */
export function IntakePanel() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [planOpen, setPlanOpen] = useState(false);
  const [capOpen, setCapOpen] = useState(false);
  const [capDraft, setCapDraft] = useState(CONTINUOUS_DAILY_CAP);
  const intake = useQuery({ queryKey: ["intake"], queryFn: api.intake, refetchInterval: 3000 });

  const action = useMutation({
    mutationFn: (kind: "pause" | "resume" | "stop") =>
      kind === "pause" ? api.pauseIntake() : kind === "resume" ? api.resumeIntake() : api.stopIntake(true),
    onSuccess: (_, kind) => {
      const message =
        kind === "stop"
          ? "生产计划已终止，未开工工程已归还"
          : kind === "pause"
            ? "生产计划已暂停"
            : "生产计划已继续，中断任务已重新拉起";
      toast(message, "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const toContinuous = useMutation({
    mutationFn: () => {
      const plan = intake.data?.plan;
      if (!plan) throw new Error("当前没有生产计划");
      return api.createIntake({
        project_id: plan.project_id,
        mode: "continuous",
        daily_cap: CONTINUOUS_DAILY_CAP,
        filters: plan.filters,
        exclude_delivered: Boolean(plan.exclude_delivered),
        max_uses: plan.max_uses ?? 0,
      });
    },
    onSuccess: () => {
      toast(`已转为持续生产：题库循环复用，每日最多领取 ${CONTINUOUS_DAILY_CAP} 次`, "ok");
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  // 只改每日领取上限，筛选、复用规则等其余参数按当前计划原样带回。
  const updateCap = useMutation({
    mutationFn: (cap: number) => {
      const plan = intake.data?.plan;
      if (!plan) throw new Error("当前没有生产计划");
      return api.createIntake({
        project_id: plan.project_id,
        mode: "continuous",
        daily_cap: cap,
        filters: plan.filters,
        exclude_delivered: Boolean(plan.exclude_delivered),
        max_uses: plan.max_uses ?? 0,
      });
    },
    onSuccess: (_, cap) => {
      toast(`每日领取上限已改为 ${cap} 次`, "ok");
      setCapOpen(false);
      void queryClient.invalidateQueries();
    },
    onError: (error) => toast(errorText(error), "error"),
  });

  const data = intake.data;
  const plan = data?.plan;
  const active = Boolean(plan && ["running", "paused"].includes(plan.status));
  const continuous = plan?.mode === "continuous";
  const counts = data?.counts;
  const daily = data?.daily;
  const progress = !plan
    ? 0
    : continuous
      ? Math.min(100, Math.round(((daily?.claimed ?? 0) / Math.max(1, daily?.cap ?? 1)) * 100))
      : Math.min(100, Math.round(((counts?.completed ?? 0) / Math.max(1, plan.target)) * 100));

  return (
    <Panel
      title="生产计划"
      actions={
        !active ? (
          <Button size="small" variant="primary" onClick={() => setPlanOpen(true)}>
            <WandSparkles size={13} /> 下达计划
          </Button>
        ) : null
      }
    >
      <div className="panel-body dispatch-plan">
        {!plan || !active ? (
          <span className="dispatch-plan-empty">
            {plan ? `上一个计划${STATUS_LABEL[plan.status] ?? plan.status}。` : "当前没有生产计划，调度只处理已加入队列的任务。"}
            下达计划后会按在手上限补位领取，不会一次囤积。
          </span>
        ) : (
          <>
            <strong>
              {continuous ? "持续生产" : `定量 ${plan.target} 题`} · {STATUS_LABEL[plan.status] ?? plan.status}
            </strong>
            <span className="dispatch-plan-meta">
              {continuous ? `今日领取 ${daily?.claimed ?? 0}/${daily?.cap ?? 0}` : `完成 ${counts?.completed ?? 0}/${plan.target}`}
              {" · "}进行中 {counts?.active ?? 0} · 失败 {counts?.failed ?? 0}
              {counts?.recoverable ? ` · 可恢复 ${counts.recoverable}` : ""}
            </span>
            <div className="intake-progress"><span style={{ width: `${progress}%` }} /></div>
            <span className="dispatch-plan-meta">
              {continuous
                ? "题库循环复用，同工程不出雷同题"
                : data?.eta
                  ? `预计 ${formatDateTime(data.eta)} 完成`
                  : "样本不足 3 题，不估算完成时间"}
            </span>
            <span className="dispatch-plan-meta">
              默认轮转该项目全部题目
              {data?.disabled_count ? `，已禁用 ${data.disabled_count} 题` : ""} · <Link className="dispatch-link" to="/source">去题源管理 →</Link>
            </span>
            {plan.status === "paused" && plan.pause_reason ? <p className="danger-text">{plan.pause_reason}</p> : null}
            {plan.status === "running" && plan.waiting_reason ? <p className="form-note">{plan.waiting_reason}</p> : null}
            <div className="row-actions dispatch-plan-actions">
              {!continuous ? (
                <Button
                  size="small"
                  variant="primary"
                  onClick={() => toContinuous.mutate()}
                  loading={toContinuous.isPending}
                  title="保留已有任务，改为不设终点、每日限量领取，重启后自动续跑"
                >
                  转为持续生产
                </Button>
              ) : null}
              {continuous ? (
                <Button
                  size="small"
                  onClick={() => {
                    setCapDraft(daily?.cap || plan.daily_cap || CONTINUOUS_DAILY_CAP);
                    setCapOpen(true);
                  }}
                  title="修改持续生产每天最多领取的次数，立即生效"
                >
                  调整每日上限
                </Button>
              ) : null}
              {plan.status === "running" ? (
                <Button size="small" onClick={() => action.mutate("pause")} loading={action.isPending}>暂停计划</Button>
              ) : (
                <Button size="small" variant={continuous ? "primary" : "secondary"} onClick={() => action.mutate("resume")} loading={action.isPending}>
                  继续计划
                </Button>
              )}
              <Button size="small" variant="danger" onClick={() => action.mutate("stop")} loading={action.isPending}>
                终止并归还未开工
              </Button>
            </div>
          </>
        )}
      </div>
      <Modal
        open={capOpen}
        title="调整每日领取上限"
        onClose={() => setCapOpen(false)}
        footer={
          <>
            <Button onClick={() => setCapOpen(false)}>取消</Button>
            <Button
              variant="primary"
              disabled={!Number.isInteger(capDraft) || capDraft < 1 || capDraft > 10000}
              loading={updateCap.isPending}
              onClick={() => updateCap.mutate(capDraft)}
            >
              保存
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <Field label="每日领取上限" hint={`今日已领取 ${daily?.claimed ?? 0} 次；达到上限后停止领取，次日 00:00 自动继续`}>
            <input type="number" min={1} max={10000} value={capDraft} onChange={(event) => setCapDraft(Number(event.target.value))} />
          </Field>
        </div>
        <p className="form-note">只修改上限，筛选条件和复用规则保持不变。调高后如果在手未满，会立即继续领取。</p>
      </Modal>
      <PlanModal
        open={planOpen}
        onClose={() => setPlanOpen(false)}
        onCreated={() => {
          setPlanOpen(false);
          void queryClient.invalidateQueries();
        }}
      />
    </Panel>
  );
}

export function PlanModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { toast } = useToast();
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api.projects(), enabled: open });
  // 默认用题源页当前选中的项目，该项目下的题全部参与轮转。
  const [projectId, setProjectId] = useState(() => {
    try {
      return localStorage.getItem("solo-auto-project") ?? "";
    } catch {
      return "";
    }
  });
  const [mode, setMode] = useState<"continuous" | "target">("continuous");
  const [target, setTarget] = useState(15);
  const [dailyCap, setDailyCap] = useState(CONTINUOUS_DAILY_CAP);
  const [q, setQ] = useState("");
  const [language, setLanguage] = useState("");
  const [maxUses, setMaxUses] = useState(0);
  const [excludeDelivered, setExcludeDelivered] = useState(false);
  useEffect(() => {
    const items = projects.data?.items ?? [];
    if (items.length && !items.some((project) => project.id === projectId)) setProjectId(items[0].id);
  }, [projectId, projects.data?.items]);
  const create = useMutation({
    mutationFn: () =>
      api.createIntake({
        project_id: projectId,
        mode,
        target: mode === "target" ? target : 0,
        daily_cap: dailyCap,
        filters: { q, language },
        exclude_delivered: excludeDelivered,
        max_uses: maxUses,
      }),
    onSuccess: () => {
      toast(mode === "continuous" ? "持续生产已启动，题库将循环复用" : "生产计划已启动，将按在手上限补位领取", "ok");
      onCreated();
    },
    onError: (error) => toast(errorText(error), "error"),
  });
  return (
    <Modal
      open={open}
      title="下达生产计划"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!projectId || (mode === "target" ? target < 1 : dailyCap < 1)} loading={create.isPending} onClick={() => create.mutate()}>
            下达计划
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field label="项目" hint="默认是题源页当前选中的项目，项目下的题都可以用">
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">请选择项目</option>
            {projects.data?.items.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </Field>
        <Field label="模式" hint="持续生产不设终点，重启后自动续跑">
          <select value={mode} onChange={(event) => setMode(event.target.value as "continuous" | "target")}>
            <option value="continuous">持续生产（循环题库）</option>
            <option value="target">交付 N 题后结束</option>
          </select>
        </Field>
        {mode === "target" ? (
          <Field label="目标题数">
            <input type="number" min={1} max={15} value={target} onChange={(event) => setTarget(Number(event.target.value))} />
          </Field>
        ) : (
          <Field label="每日领取上限" hint="达到后停止领取，次日 00:00 自动继续">
            <input type="number" min={1} max={10000} value={dailyCap} onChange={(event) => setDailyCap(Number(event.target.value))} />
          </Field>
        )}
        <Field label="工程名筛选" hint="留空即使用全部题；单道题不想用，去题源页禁用">
          <input value={q} onChange={(event) => setQ(event.target.value)} placeholder="可留空" />
        </Field>
        <Field label="语言">
          <input value={language} onChange={(event) => setLanguage(event.target.value)} placeholder="如 Python" />
        </Field>
        <Field label="最大历史使用次数" hint="0 表示允许复用；达到目标前优先使用次数较少的工程">
          <input type="number" min={0} max={100} value={maxUses} onChange={(event) => setMaxUses(Number(event.target.value))} />
        </Field>
      </div>
      <label className="check-inline">
        <input
          type="checkbox"
          checked={excludeDelivered}
          onChange={(event) => setExcludeDelivered(event.target.checked)}
        />
        排除已经交付过的工程
      </label>
      <p className="form-note">
        {mode === "continuous"
          ? "按在手上限补位，任务结束即归还原工程；按使用次数从少到多、同次数先用最久未用的工程，整库轮转。复用同一工程时会把它出过的题面交给出题 Agent 避开，导出前再查一次雷同。已有同项目计划时会直接转为持续生产，保留已领取的任务。"
          : "单项目最多 15 题。先接回已领取但未建题的材料，再按在手上限补位；任务结束后归还原工程，并在需要时按使用次数从少到多复用。"}
      </p>
    </Modal>
  );
}
