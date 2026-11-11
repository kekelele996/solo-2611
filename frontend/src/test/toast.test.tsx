import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../components/toast";
import { useToast } from "../components/toast-context";

function Trigger({ undo }: { undo: () => void }) {
  const { toast } = useToast();
  return (
    <>
      <button onClick={() => toast("same error", "error")}>repeat</button>
      <button onClick={() => toast("another error", "error")}>another</button>
      <button onClick={() => toast("cleaned", "ok", { label: "撤销", onClick: undo })}>with action</button>
    </>
  );
}

describe("toast lifecycle", () => {
  afterEach(cleanup);

  it("deduplicates repeated messages and supports clearing all", () => {
    render(
      <ToastProvider>
        <Trigger undo={() => {}} />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByText("repeat"));
    fireEvent.click(screen.getByText("repeat"));
    fireEvent.click(screen.getByText("another"));

    expect(screen.getAllByText("same error")).toHaveLength(1);
    expect(screen.getByText("another error")).toBeInTheDocument();

    fireEvent.click(screen.getByText("全部清除"));
    expect(screen.queryByText("same error")).not.toBeInTheDocument();
    expect(screen.queryByText("another error")).not.toBeInTheDocument();
  });

  it("runs and clears an undo action", async () => {
    const undo = vi.fn();
    render(
      <ToastProvider>
        <Trigger undo={undo} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("with action"));
    fireEvent.click(screen.getByText("撤销"));
    expect(undo).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByText("cleaned")).not.toBeInTheDocument());
  });
});
