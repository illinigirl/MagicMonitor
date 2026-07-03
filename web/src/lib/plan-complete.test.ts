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
