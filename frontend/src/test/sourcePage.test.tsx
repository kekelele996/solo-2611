import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { ToastProvider } from "../components/toast";
import { SourcePage } from "../pages/SourcePage";

describe("SourcePage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(api, "projects").mockResolvedValue({ items: [], total: 0 });
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

  it("keeps the prompt library in its own source tab", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/source"]}>
          <ToastProvider>
            <SourcePage />
          </ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

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
