import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { PasswordField } from "../components/PasswordField";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("drops a pending reveal when switching profiles", async () => {
  let finish!: (value: { password: string }) => void;
  const response = new Promise<{ password: string }>(resolve => { finish = resolve; });
  const reveal = vi.spyOn(api, "revealPassword").mockReturnValue(response);
  const saved = { profile: "first", token: "test-session", target: "solo2" as const };
  const view = render(<PasswordField label="密码" saved={saved} readOnly />);
  fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
  view.rerender(<PasswordField label="密码" saved={{ ...saved, profile: "second" }} readOnly />);
  expect(reveal.mock.calls[0][3].aborted).toBe(true);
  await act(async () => { finish({ password: "fixture-old" }); await response; });
  expect(screen.getByLabelText("密码")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("密码")).toHaveValue("");
});
