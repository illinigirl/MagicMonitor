// Pin the WMO-code → label/icon buckets (a wrong bucket = a wrong glance).
import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { _describe } from "./weather";

describe("weather code mapping", () => {
  it.each([
    [0, "Clear"],
    [2, "Partly cloudy"],
    [3, "Overcast"],
    [45, "Fog"],
    [55, "Drizzle"],
    [63, "Rain"],
    [81, "Showers"],
    [95, "Thunderstorm"],
    [99, "Thunderstorm"],
  ])("code %i → %s", (code, label) => {
    expect(_describe(code as number).condition).toBe(label);
  });

  it("unknown codes degrade to a dash, not a crash", () => {
    expect(_describe(-1).condition).toBe("—");
  });
});
