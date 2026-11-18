import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { EngineeringPools, ProfileCatalog } from "../api/types";
import { StandbyPoolEditor } from "../components/source/StandbyPoolEditor";
import { ToastProvider } from "../components/toast";

const PROFILES: ProfileCatalog = {
  active: "default",
  items: [{ id: "default", name: "刘东" }],
  connections: [],
  standby: { username: "hexin", domain: "数据工程", revision: "revision-1" },
};

const POOLS: EngineeringPools = {
  primary_project_id: "pool-a",
  primary_project_name: "主号项目池",
  primary_source_address: "http://hub.example:8088",
  standby_project_id: "pool-b",
  standby_project_name: "副号项目池",
  standby_source_address: "",
  standby_source_user: "",
  standby_source_password: "",
  standby_use_primary_source: false,
};

describe("StandbyPoolEditor", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "viewCodeStatus").mockResolvedValue({ configured: true });
    vi.spyOn(api, "unlockAccounts").mockResolvedValue({ connections: [], expires_in: 300, token: "view-token" });
    vi.spyOn(api, "lockAccounts").mockResolvedValue(undefined);
    vi.spyOn(api, "profiles").mockResolvedValue(PROFILES);
  });
  afterEach(cleanup);

  it("uses the configured engineering address and exposes a plain secondary password field", async () => {
    vi.spyOn(api, "engineeringPools").mockResolvedValue(POOLS);
    renderEditor();

    fireEvent.change(await screen.findByLabelText("查看码"), { target: { value: "view-code" } });
    fireEvent.click(screen.getByRole("button", { name: "解锁查看" }));

    const address = await screen.findByLabelText(/^工程台地址/);
    expect(address).toHaveValue("http://hub.example:8088");
    expect(screen.getByLabelText(/^副号工程台账号/)).toHaveValue("");
    expect(screen.getByLabelText(/^副号工程台密码/)).toHaveAttribute("type", "text");
    expect(screen.queryByText("使用独立工程台账号")).toBeNull();
    expect(screen.getByText(/默认使用「设置 → 账号与连接」里的题源地址/)).toBeVisible();
  });

  it("prefills the saved custom address, account and password", async () => {
    const saved: EngineeringPools = {
      ...POOLS,
      standby_source_address: "http://10.0.0.5:8088",
      standby_source_user: "hexin-source",
      standby_source_password: "not-sensitive",
    };
    vi.spyOn(api, "engineeringPools").mockResolvedValue(saved);
    const save = vi.spyOn(api, "saveEngineeringPools").mockResolvedValue(saved);
    const onClose = vi.fn();
    renderEditor(onClose);

    fireEvent.change(await screen.findByLabelText("查看码"), { target: { value: "view-code" } });
    fireEvent.click(screen.getByRole("button", { name: "解锁查看" }));

    const address = await screen.findByLabelText(/^工程台地址/);
    await waitFor(() => expect(address).toHaveValue("http://10.0.0.5:8088"));
    expect(screen.getByLabelText(/^副号工程台账号/)).toHaveValue("hexin-source");
    expect(screen.getByLabelText(/^副号工程台密码/)).toHaveValue("not-sensitive");
    expect(screen.getByRole("combobox", { name: "副号项目组" })).toHaveValue("pool-b");

    fireEvent.click(screen.getByRole("button", { name: "保存副号项目池" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("view-token", "default", {
      standby_use_primary_source: false,
      standby_source_address: "http://10.0.0.5:8088",
      standby_source_user: "hexin-source",
      standby_source_password: "not-sensitive",
      primary_project_id: "pool-a",
      standby_project_id: "pool-b",
    }));
    expect(onClose).toHaveBeenCalled();
  });
});

function renderEditor(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ToastProvider>
          <StandbyPoolEditor primaryId="pool-a" projects={[]} onClose={onClose} />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
