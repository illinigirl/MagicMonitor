import { describe, expect, it } from "vitest";

import { formatEtTime, parseEtTime } from "./format-et";

describe("parseEtTime (TS mirror of mcp parse_ll_time)", () => {
  const D = "2026-07-04";

  it.each([
    ["3:15 PM", "2026-07-04T15:15:00-04:00"],
    ["3pm", "2026-07-04T15:00:00-04:00"],
    ["15:15", "2026-07-04T15:15:00-04:00"],
    ["12:00 AM", "2026-07-04T00:00:00-04:00"],
    ["12:30 PM", "2026-07-04T12:30:00-04:00"],
  ])("%s → %s", (input, expected) => {
    expect(parseEtTime(input, D)).toBe(expected);
  });

  it("passes full ISO with offset through", () => {
    expect(parseEtTime("2026-07-04T15:15:00-04:00", D)).toBe(
      "2026-07-04T15:15:00-04:00",
    );
  });

  it("winter dates get the EST offset", () => {
    expect(parseEtTime("3:15 PM", "2026-01-15")).toBe(
      "2026-01-15T15:15:00-05:00",
    );
  });

  it.each([["brunchish"], [""], ["25:00"], ["9:75"]])(
    "unparseable %s → null",
    (input) => {
      expect(parseEtTime(input, D)).toBeNull();
    },
  );

  it("round-trips with formatEtTime", () => {
    expect(formatEtTime(parseEtTime("3:15 PM", D)!)).toBe("3:15 PM");
  });
});
