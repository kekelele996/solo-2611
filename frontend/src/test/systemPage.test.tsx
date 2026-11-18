import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountProfiles } from "../components/AccountProfiles";
import { api } from "../api/client";
import type { ConfigResponse, ProfileCatalog } from "../api/types";
import { ToastProvider } from "../components/toast";
import { SystemPage } from "../pages/SystemPage";

const CONFIG: ConfigResponse = {
  host: "127.0.0.1",
  port: 8099,
  skill_root: "/skills/solo-2611",
  skill_version: "0.2.3",
  tasks_dir: "/tasks",
  source: {
    base_url: "http://hub",
    auth_mode: "cookie",
    username: "u",
    timeout_seconds: 20,
    password: "<set>",
    bearer_token: "<unset>",
  },
  execution: {
    max_containers: 4,
    run_timeout_seconds: 10800,
    attempts: 1,
    run_mode: "supervised",
    open_terminal_on_run: false,
    continue_max: 2,
    start_attempts: 2,
    silent_seconds: 1200,
    no_progress_seconds: 1200,
    fatal_hold_minutes: 30,
    stale_after_seconds: 5400,
  },
  storage: { disk_floor_gb: 30, event_retention_days: 30, skill_log_retention_days: 14 },
  recording: { max_seconds: 90, chrome_url: "http://127.0.0.1:3000" },
  submission: { auto_submit: false, poll_interval_seconds: 300, poll_timeout_seconds: 86400 },
  autopilot: {
    enabled: true,
    agent_mode: "chatgpt",
    agent_bin: "",
    agent_args: [],
    chatgpt_folder_id: "",
    chatgpt_push_helper: "",
    chatgpt_timeout_seconds: 1800,
    task_timeout_seconds: 7200,
    restricted: false,
    stage_timeout_seconds: 3600,
    stage_attempts: 2,
    run_attempts: 2,
    parallel_authoring: 2,
    parallel_delivering: 1,
    stop_kills_containers: true,
    breaker: { same_kind_failures: 3, audit_reject_rate: 0.2, audit_reject_window: 10 },
  },
  audit: { mode: "sample", sample_rate: 0.3, stratify_by: "task_type" },
  intake: { max_in_hand: 0, exclude_tags: [] },
  trash: { retention_days: 7 },
  slim: { enabled: false, after_days: 3, patterns: ["node_modules", ".venv"] },
  notify: { macos: true, webhook_url: "" },
  solo2: {
    base_url: "https://solo2.jzxhnh.com/app",
    username: "reviewer",
    timeout_seconds: 30,
    password: "<set>",
  },
  skill_config: {},
  gateway_key: "<unset>",
  model_base_url: "https://gateway.test",
  github_token: "<set>",
};

function field(label: string): HTMLInputElement {
  const node = screen.getByText(label).closest("label")?.control;
  if (!(node instanceof HTMLInputElement)) throw new Error(`找不到字段：${label}`);
  return node;
}

describe("settings page", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/system");
    vi.restoreAllMocks();
    vi.spyOn(api, "viewCodeStatus").mockResolvedValue({ configured: false });
    vi.spyOn(api, "config").mockResolvedValue(CONFIG);
    vi.spyOn(api, "logs").mockResolvedValue({ items: [] });
    vi.spyOn(api, "systemStatus").mockResolvedValue({} as Awaited<ReturnType<typeof api.systemStatus>>);
    vi.spyOn(api, "trash").mockResolvedValue({ items: [], total: 0, total_bytes: 0, retention_days: 7 });
  });

  afterEach(cleanup);

  it("edits one standby under each primary and drops drafts when switching", async () => {
    const items = [{ id: "a", name: "主号 A" }, { id: "b", name: "主号 B" }];
    let active = "a";
    const saved: Record<string, ProfileCatalog["standby"]> = {};
    const catalog = (): ProfileCatalog => ({ active, items, standby: saved[active], connections: [
      { name: "SOLO2 提交平台", address: "https://solo.example", account: `main-${active}`, credential_saved: true },
    ] });
    vi.spyOn(api, "profiles").mockImplementation(() => Promise.resolve(catalog()));
    vi.spyOn(api, "activateProfile").mockImplementation((_token, id) => { active = id; return Promise.resolve(catalog()); });
    const save = vi.spyOn(api, "saveStandby").mockImplementation((_token, id, draft) => {
      saved[id] = { username: draft.username, domain: draft.domain, revision: id };
      return Promise.resolve(catalog());
    });
    save.mockRejectedValueOnce(new Error("副号配置保存后无法核对，请重试"));
    const test = vi.spyOn(api, "testStandby").mockResolvedValue({
      solo2: { ok: true, message: "SOLO2 测试成功" },
      github: { ok: false, message: "GitHub 认证失败（HTTP 401）" },
    });
    const create = vi.spyOn(api, "createProfile");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ToastProvider><AccountProfiles token="test-session" canCreate /></ToastProvider></QueryClientProvider>);
    const selector = await screen.findByRole("combobox", { name: "当前主号" });
    for (const id of ["a", "b"]) {
      await screen.findByRole("region", { name: `main-${id} 的副号` });
      expect(screen.getByText("尚未绑定副号")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "绑定副号" }));
      for (const [label, value] of [["副号 SOLO2 账号", `alt-${id}`], ["副号 SOLO2 密码", "test-password"],
          ["副号绑定领域", "数据工程"], ["副号专属 GitHub 密钥", "test-token"]]) {
        fireEvent.change(screen.getByLabelText(label), { target: { value } });
      }
      if (id === "a") {
        fireEvent.click(screen.getByRole("button", { name: "测试副号连接" }));
        expect(await screen.findByText("SOLO2 测试成功")).toBeVisible();
        expect(screen.getByText("GitHub 认证失败（HTTP 401）")).toBeVisible();
        expect(test.mock.lastCall).toMatchObject(["test-session", id, { username: "alt-a", revision: "" }]);
        expect(save).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole("button", { name: "保存副号" }));
        expect(await screen.findByText("副号配置保存后无法核对，请重试")).toBeVisible();
        expect(screen.getByLabelText("副号 SOLO2 账号")).toHaveValue("alt-a");
      }
      fireEvent.click(screen.getByRole("button", { name: "保存副号" }));
      await screen.findByText(`alt-${id} · 数据工程`);
      const [token, parent, draft] = save.mock.lastCall!;
      expect([token, parent]).toEqual(["test-session", id]);
      expect(draft).toMatchObject({ username: `alt-${id}`, primary_username: `main-${id}`,
        password: "test-password", github_token: "test-token", domain: "数据工程" });
      fireEvent.click(screen.getByRole("button", { name: "测试副号连接" }));
      await screen.findByText("SOLO2 测试成功");
      expect(test.mock.lastCall).toEqual(["test-session", id, {
        username: `alt-${id}`, primary_username: `main-${id}`, primary_address: "https://solo.example",
        revision: id, domain: "数据工程", password: "", github_token: "",
      }]);
      fireEvent.click(screen.getByRole("button", { name: "编辑副号" }));
      fireEvent.change(screen.getByLabelText("副号 SOLO2 账号"), { target: { value: "unsaved" } });
      fireEvent.change(selector, { target: { value: id === "a" ? "b" : "a" } });
    }
    await screen.findByText("alt-a · 数据工程");
    expect(screen.queryByText("alt-b · 数据工程")).toBeNull();
    expect(screen.queryByDisplayValue("unsaved")).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(create).not.toHaveBeenCalled();
  });

  // 每个可编辑字段改动后都必须能保存，并且不会被后台刷新冲掉
  it.each([
    ["脚本目录", "/skills/other", (body: Record<string, unknown>) => body.skill_root],
    ["超时秒数", "3600", (body: Record<string, { [key: string]: unknown }>) => body.execution.run_timeout_seconds],
    ["继续上限", "1", (body: Record<string, { [key: string]: unknown }>) => body.execution.continue_max],
    ["任务目录", "/other", (body: Record<string, unknown>) => body.tasks_dir],
    ["磁盘阈值 GB", "10", (body: Record<string, { [key: string]: unknown }>) => body.storage.disk_floor_gb],
    ["Chrome URL", "http://127.0.0.1:5000", (body: Record<string, { [key: string]: unknown }>) => body.recording.chrome_url],
    ["平台地址", "https://solo2.example/app", (body: Record<string, { [key: string]: unknown }>) => body.solo2.base_url],
    ["平台账号", "new-user", (body: Record<string, { [key: string]: unknown }>) => body.solo2.username],
    ["任务总超时（秒）", "8100", (body: Record<string, { [key: string]: unknown }>) => body.autopilot.task_timeout_seconds],
  ])("saves an edit to %s", async (label, value, pick) => {
    const patch = vi.spyOn(api, "patchConfig").mockResolvedValue(CONFIG);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    const save = await screen.findByRole("button", { name: "配置已保存" });
    expect(save).toBeDisabled();

    if (label !== "平台账号" && label !== "平台地址") fireEvent.click(screen.getByRole("tab", {name: "运行与交付"}));
    fireEvent.change(field(label), { target: { value } });
    await client.invalidateQueries();
    expect(field(label).value).toBe(value);

    const button = screen.getByRole("button", { name: "保存配置" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(String(pick(patch.mock.calls[0][0] as never))).toBe(value);
    await screen.findByRole("button", { name: "配置已保存" });
  });

  it("shows all connection fields and only loads maintenance data in maintenance", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ToastProvider><SystemPage /></ToastProvider></QueryClientProvider>);
    await screen.findByRole("heading", { name: "SOLO2 提交平台" });
    expect(await screen.findByRole("button", {name: "保存查看码"})).toBeVisible();
    expect(field("平台密码")).toBeVisible();
    expect(field("题源地址")).toBeVisible();
    expect(field("GitHub 凭据")).toBeVisible();
    expect(field("网关地址")).toBeVisible();
    expect(api.logs).not.toHaveBeenCalled();
    expect(api.trash).not.toHaveBeenCalled();
    fireEvent.change(field("平台账号"), { target: { value: "other" } });
    fireEvent.click(screen.getByRole("tab", { name: "维护与日志" }));
    await waitFor(() => expect(api.logs).toHaveBeenCalled());
    expect(api.trash).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "账号与连接" }));
    expect(field("平台账号").value).toBe("other");
  });

  it("shows the active profile and saves edits against the selected profile", async () => {
    const items = [{ id: "default", name: "默认配置" }, { id: "other", name: "备用配置" }];
    let active = "default";
    const profiles = () => ({ active, items, connections: [] });
    const config = () => ({ ...CONFIG, active_profile: active, solo2: { ...CONFIG.solo2, username: active } });
    vi.mocked(api.viewCodeStatus).mockResolvedValue({ configured: true });
    vi.mocked(api.config).mockImplementation(token => Promise.resolve(token ? config() : { ...CONFIG, accounts_locked: true }));
    vi.spyOn(api, "unlockAccounts").mockResolvedValue({ token: "test-session", expires_in: 300, connections: [] });
    vi.spyOn(api, "lockAccounts").mockResolvedValue(undefined);
    vi.spyOn(api, "profiles").mockImplementation(() => Promise.resolve(profiles()));
    const sample = "example-credential";
    const reveal = vi.spyOn(api, "revealPassword").mockResolvedValue({ password: sample });
    const activate = vi.spyOn(api, "activateProfile").mockImplementation((_token, id) => {
      active = id;
      return Promise.resolve(profiles());
    });
    const patch = vi.spyOn(api, "patchConfig").mockImplementation(() => Promise.resolve(config()));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}><ToastProvider><SystemPage /></ToastProvider></QueryClientProvider>);
    const unlock = await screen.findByRole("button", { name: "解锁查看" });
    expect(screen.queryByText("平台账号")).not.toBeInTheDocument();
    fireEvent.change(field("查看码"), { target: { value: "test-code" } });
    fireEvent.click(unlock);

    const select = await screen.findByRole("combobox", { name: "当前主号" });
    expect(select).toHaveValue("default");
    expect(screen.getByText("正在使用")).toBeVisible();
    fireEvent.change(select, { target: { value: "other" } });
    await waitFor(() => expect(field("平台账号")).toHaveValue("other"));
    expect(activate).toHaveBeenCalledWith("test-session", "other");
    expect(select).toHaveValue("other");

    const github = field("GitHub 凭据");
    expect(github).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示GitHub 凭据" }));
    await waitFor(() => expect(github).toHaveValue(sample));
    expect(github).toHaveAttribute("type", "text");
    expect(reveal.mock.calls[0].slice(0, 3)).toEqual(["test-session", "other", "github"]);
    fireEvent.click(screen.getByRole("button", { name: "隐藏GitHub 凭据" }));
    expect(github).toHaveAttribute("type", "password");
    expect(github).toHaveValue("");

    fireEvent.change(field("平台账号"), { target: { value: "updated" } });
    expect(select).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const [payload, token, profile] = patch.mock.calls[0];
    expect(payload.solo2?.username).toBe("updated");
    expect(payload.secrets).toBeUndefined();
    expect([token, profile]).toEqual(["test-session", "other"]);
  });

  it("stores the Solo2 password as a secret", async () => {
    const patch = vi.spyOn(api, "patchConfig").mockResolvedValue(CONFIG);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    await screen.findByText("SOLO2 提交平台");
    fireEvent.change(field("平台密码"), { target: { value: "new-password" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect((patch.mock.calls[0][0] as { secrets?: { solo2_password?: string } }).secrets?.solo2_password).toBe(
      "new-password",
    );
  });

  it("saves the automatic SOLO2 submission switch", async () => {
    const patch = vi.spyOn(api, "patchConfig").mockResolvedValue(CONFIG);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("tab", {name: "运行与交付"}));
    const toggle = await screen.findByRole("checkbox", { name: /自动提交到 SOLO2/ });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(
      (patch.mock.calls[0][0] as { submission?: { auto_submit?: boolean } }).submission?.auto_submit,
    ).toBe(true);
  });

  it("saves the visible terminal switch", async () => {
    const patch = vi.spyOn(api, "patchConfig").mockResolvedValue(CONFIG);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("tab", {name: "运行与交付"}));
    const toggle = await screen.findByRole("checkbox", { name: /启动时打开终端窗口/ });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(
      (patch.mock.calls[0][0] as { execution?: { open_terminal_on_run?: boolean } }).execution
        ?.open_terminal_on_run,
    ).toBe(true);
  });

  it("probes the Claude Code API independently", async () => {
    const probe = vi.spyOn(api, "testConfig").mockResolvedValue({
      ok: true,
      message: "配置测试成功 HTTP 200",
      detail: {},
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SystemPage />
        </ToastProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "探测 API" }));
    await waitFor(() => expect(probe).toHaveBeenCalledWith("api"));
    await screen.findByText("配置测试成功 HTTP 200");
  });
});
