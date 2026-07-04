import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { pickNextUp, tokenMatches } from "./plan-complete";

const rides = [
  { ride_id: "space", ride_name: "Space Mountain" },
  { ride_id: "tron", ride_name: "TRON" },
  { ride_id: "buzz", ride_name: "Buzz Lightyear" },
  { ride_id: "hm", ride_name: "Haunted Mansion" },
];

describe("pickNextUp", () => {
  it("advances to the first remaining ride in plan order", () => {
    expect(pickNextUp(rides, [], [], "space")?.ride_id).toBe("tron");
  });

  it("skips completed and dropped rides", () => {
    expect(
      pickNextUp(rides, ["tron"], ["buzz"], "space")?.ride_id,
    ).toBe("hm");
  });

  it("returns null when the plan is finished", () => {
    expect(pickNextUp(rides, ["space", "tron"], ["buzz"], "hm")).toBeNull();
  });

  it("never advances to the ride just done, even if not yet in completed", () => {
    // completeRideAndAdvance decides from a row read BEFORE its write,
    // so the just-done ride won't appear in completed_ride_ids yet.
    expect(pickNextUp([rides[0]], [], [], "space")).toBeNull();
  });

  it("handles an empty plan", () => {
    expect(pickNextUp([], [], [], "space")).toBeNull();
  });
});

describe("tokenMatches", () => {
  it("accepts the exact token", () => {
    expect(tokenMatches("abc123", "abc123")).toBe(true);
  });

  it.each([
    ["wrong content", "abc123", "abc124"],
    ["length mismatch", "abc123", "abc1234"],
    ["empty provided", "abc123", ""],
    ["null expected (never provisioned)", null, "abc123"],
    ["both empty", "", ""],
  ])("rejects %s", (_label, expected, provided) => {
    expect(tokenMatches(expected, provided)).toBe(false);
  });
});

describe("insertIntoPlanOrder (un-drop re-slots by time, 2026-07-04)", () => {
  const rides = [
    { ride_id: "nemo", target_time: "2026-07-04T11:25:00-04:00" },
    { ride_id: "sse", target_time: "2026-07-04T12:45:00-04:00" },
    { ride_id: "tt", target_time: "2026-07-04T13:15:00-04:00" },
    { ride_id: "gotg", target_time: "2026-07-04T14:30:00-04:00" },
  ];

  it("inserts before the first LATER-timed ranked ride", async () => {
    const { insertIntoPlanOrder } = await import("./plan-complete");
    expect(insertIntoPlanOrder(["nemo", "sse", "gotg"], rides, "tt")).toEqual([
      "nemo", "sse", "tt", "gotg",
    ]);
  });

  it("appends when nothing ranked is later, or the ride has no time", async () => {
    const { insertIntoPlanOrder } = await import("./plan-complete");
    expect(insertIntoPlanOrder(["nemo", "sse"], rides, "gotg")).toEqual([
      "nemo", "sse", "gotg",
    ]);
    expect(
      insertIntoPlanOrder(["nemo", "sse"], [{ ride_id: "x" }], "x"),
    ).toEqual(["nemo", "sse", "x"]);
  });

  it("no-ops (null) when there's no order or the ride is already ranked", async () => {
    const { insertIntoPlanOrder } = await import("./plan-complete");
    expect(insertIntoPlanOrder(undefined, rides, "tt")).toBeNull();
    expect(insertIntoPlanOrder([], rides, "tt")).toBeNull();
    expect(insertIntoPlanOrder(["tt", "sse"], rides, "tt")).toBeNull();
  });
});
