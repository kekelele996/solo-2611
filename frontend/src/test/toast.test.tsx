import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ToastProvider } from "../components/toast";
import { useToast } from "../components/toast-context";

function Trigger() {
  const { toast } = useToast();
  return (
    <>
      <button onClick={() => toast("same error", "error")}>repeat</button>
      <button onClick={() => toast("another error", "error")}>another</button>
    </>
  );
}

describe("toast lifecycle", () => {
  it("deduplicates repeated messages and supports clearing all", () => {
    render(
      <ToastProvider>
        <Trigger />
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
});
