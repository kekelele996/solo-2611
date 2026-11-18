import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { IntakeSnapshot } from "../api/types";
import { ToastProvider } from "../components/toast";
import { SourcePage } from "../pages/SourcePage";

describe("SourcePage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(api, "projects").mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(api, "intake").mockResolvedValue(INTAKE_WITHOUT_PLAN);
    vi.spyOn(api, "claims").mockResolvedValue({ items: [] });
    vi.spyOn(api, "systemStatus").mockResolvedValue({
      docker_ok: true,
      source_online: true,
      offline: false,
      disk_free_gb: 100,
      disk_low: false,
      running: 0,
      queued: 0,
      blocked: 0,
      auth_blocked: 0,
      skill_version: "0.6.0",
    });
  });
  afterEach(cleanup);

  it("refreshes the dispatch queue source after choosing another project", async () => {
    (api.projects as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        { id: "project-1", name: "项目一", status: "active", engineering_count: 1 },
        { id: "project-2", name: "项目二", status: "active", engineering_count: 1 },
      ],
      total: 2,
    });
    (api.intake as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...INTAKE_WITHOUT_PLAN,
      plan: {
        id: "plan-1",
        project_id: "project-1",
        project_name: "项目一",
        filters: {},
        mode: "continuous",
        target: 0,
        status: "running",
        created_at: "2026-11-15T10:00:00+08:00",
        updated_at: "2026-11-15T10:00:00+08:00",
        task_ids: [],
        failed_claims: [],
      },
    } satisfies IntakeSnapshot);
    vi.spyOn(api, "engineering").mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.spyOn(api, "retargetIntake").mockResolvedValue({
      ...INTAKE_WITHOUT_PLAN,
      plan: {
        id: "plan-1",
        project_id: "project-2",
        project_name: "项目二",
        filters: {},
        mode: "continuous",
        target: 0,
        status: "running",
        created_at: "2026-11-15T10:00:00+08:00",
        updated_at: "2026-11-15T10:01:00+08:00",
        task_ids: [],
        failed_claims: [],
      },
    });

    renderSourcePage();
    const select = await screen.findByRole("combobox");
    await waitFor(() => expect(select).toHaveValue("project-1"));
    fireEvent.change(select, { target: { value: "project-2" } });
    await waitFor(() => expect(select).toHaveValue("project-2"));

    const button = await screen.findByRole("button", { name: /刷新调度来源/ });
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByText(/调度任务队列仍从「项目一」补题/)).toBeInTheDocument();
    fireEvent.click(button);

    await waitFor(() => expect(api.retargetIntake).toHaveBeenCalledWith("project-2"));
    expect(await screen.findByText(/调度任务队列已改为从「项目二」补入工程/)).toBeInTheDocument();
  });

  it("keeps the prompt library in its own source tab", async () => {
    renderSourcePage();

    const engineeringTab = await screen.findByRole("tab", { name: /工程列表/ });
    const promptsTab = screen.getByRole("tab", { name: /提示词库/ });
    expect(engineeringTab).toHaveAttribute("aria-selected", "true");
    expect(document.querySelector(".prompt-library-panel")).toBeNull();

    fireEvent.click(promptsTab);

    expect(promptsTab).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByText("请先选择项目。项目提示词会按具体工程归档展示。"),
    ).toBeTruthy();
    expect(document.querySelector(".prompt-library-panel")).toBeNull();
  });
});

const INTAKE_WITHOUT_PLAN: IntakeSnapshot = {
  plan: null,
  counts: {
    target: 0,
    completed: 0,
    active: 0,
    failed: 0,
    recoverable: 0,
    orphaned: 0,
    claimed: 0,
    staged: 0,
    in_hand: 0,
  },
  max_in_hand: 4,
  eta: "",
};

function renderSourcePage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/source"]}>
        <ToastProvider>
          <SourcePage />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
