import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useDraft } from "../hooks/useDraft";

describe("useDraft", () => {
  it("adopts the server value while the user has no local edits", () => {
    const { result, rerender } = renderHook(({ server }: { server?: string }) => useDraft(server), {
      initialProps: { server: undefined as string | undefined },
    });
    expect(result.current.text).toBe("");
    rerender({ server: "v1" });
    expect(result.current.text).toBe("v1");
    expect(result.current.dirty).toBe(false);
    rerender({ server: "v2" });
    expect(result.current.text).toBe("v2");
  });

  it("keeps unsaved edits when a background refetch brings a different value", () => {
    const { result, rerender } = renderHook(({ server }: { server?: string }) => useDraft(server), {
      initialProps: { server: "v1" },
    });
    act(() => result.current.setText("我正在写的内容"));
    expect(result.current.dirty).toBe(true);
    rerender({ server: "v2" });
    expect(result.current.text).toBe("我正在写的内容");
    expect(result.current.dirty).toBe(true);
  });

  it("is clean again once the saved value comes back from the server", () => {
    const { result, rerender } = renderHook(({ server }: { server?: string }) => useDraft(server), {
      initialProps: { server: "v1" },
    });
    act(() => result.current.setText("v2"));
    rerender({ server: "v2" });
    expect(result.current.dirty).toBe(false);
  });
});
