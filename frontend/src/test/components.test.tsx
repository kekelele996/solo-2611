import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Step } from "../api/types";
import { Meter, StepRail } from "../components/ui";
import { formatClock, formatDateTime } from "../utils/time";

describe("delivery components", () => {
  it("renders all eleven steps and marks the current gate", () => {
    const steps: Step[] = Array.from({ length: 11 }, (_, index) => ({
      number: index + 1,
      key: `step-${index + 1}`,
      name: `Step ${index + 1}`,
      state: index < 6 ? "done" : index === 6 ? "current" : "pending",
      detail: "",
      ok: index < 6,
    }));

    render(<StepRail steps={steps} />);

    expect(screen.getAllByText(/Step /)).toHaveLength(11);
    expect(document.querySelector('[data-current="true"]')).toHaveTextContent("Step 7");
  });

  it("turns red above the hard threshold", () => {
    const { container } = render(<Meter value={241} max={240} label="chars" />);
    expect(screen.getByText("241/240")).toBeInTheDocument();
    expect(container.querySelector(".meter")).toHaveClass("meter-danger");
  });

  it("renders UTC evidence timestamps in Asia/Shanghai", () => {
    expect(formatDateTime("2026-11-10T17:10:13+00:00", true)).toBe("2026-11-11 01:10:13");
    expect(formatClock("2026-11-10T17:10:13Z")).toBe("01:10:13");
  });
});
