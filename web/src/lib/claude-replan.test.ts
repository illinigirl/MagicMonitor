import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
// The module imports these at top level; the pure builder never touches
// them, but they must resolve for the import to succeed.
vi.mock("@anthropic-ai/sdk", () => ({ default: class {} }));
vi.mock("@aws-sdk/client-ssm", () => ({
  SSMClient: class {},
  GetParameterCommand: class {},
}));

import { buildReplanModelInput, formatPlanRideLine } from "./claude-replan";

const plan = {
  rides: [
    { ride_id: "space", ride_name: "Space Mountain", predicted_wait_min: 30 },
    { ride_id: "tron", ride_name: "TRON", predicted_wait_min: 45 },
    { ride_id: "btm", ride_name: "Big Thunder", predicted_wait_min: 25 },
    { ride_id: "tiana", ride_name: "Tiana's Bayou", predicted_wait_min: 40 },
  ],
  dropped_ride_ids: ["tron"],
  completed_ride_ids: ["space"],
  held_lls: { btm: "2026-07-03T16:00:00-04:00" },
};

const live = [
  { ride_id: "space", name: "Space Mountain", wait_mins: 35, status: "OPERATING" },
  { ride_id: "tron", name: "TRON", wait_mins: 50, status: "OPERATING" },
  { ride_id: "btm", name: "Big Thunder", wait_mins: null, status: "DOWN" },
  { ride_id: "tiana", name: "Tiana's Bayou", wait_mins: null, status: "DOWN" },
  { ride_id: "dumbo", name: "Dumbo", wait_mins: 5, status: "OPERATING" },
  { ride_id: "teacups", name: "Mad Tea Party", wait_mins: 10, status: "CLOSED" },
];

describe("buildReplanModelInput", () => {
  it("excludes COMPLETED rides from the remaining set (2026-07-03 bug)", () => {
    const { rides } = buildReplanModelInput(plan, live);
    expect(rides.map((r) => r.ride_id)).not.toContain("space");
  });

  it("excludes dropped rides from the remaining set", () => {
    const { rides } = buildReplanModelInput(plan, live);
    expect(rides.map((r) => r.ride_id)).not.toContain("tron");
  });

  it("keeps genuinely remaining rides with live status + held LL", () => {
    const { rides } = buildReplanModelInput(plan, live);
    expect(rides.map((r) => r.ride_id).sort()).toEqual(["btm", "tiana"]);
    const btm = rides.find((r) => r.ride_id === "btm")!;
    expect(btm.status).toBe("DOWN");
    expect(btm.held_ll).toBe("2026-07-03T16:00:00-04:00");
  });

  it("reports completed rides by NAME as context", () => {
    const { completed_names } = buildReplanModelInput(plan, live);
    expect(completed_names).toEqual(["Space Mountain"]);
  });

  it("catalog = park rides not in the plan, CLOSED excluded, DOWN kept", () => {
    const { catalog } = buildReplanModelInput(plan, live);
    expect(catalog.map((c) => c.ride_id)).toEqual(["dumbo"]);
  });

  it("catalog excludes even completed plan rides (no re-ride suggestions)", () => {
    const { catalog } = buildReplanModelInput(plan, live);
    expect(catalog.map((c) => c.ride_id)).not.toContain("space");
  });

  it("plan ride prompt lines carry the [ride_id] the schema demands", () => {
    // 2026-07-03 bug #2: plan lines were name-only, so the model could
    // never emit a valid drop/order id for a planned ride — the
    // validator silently discarded every drop it proposed.
    const line = formatPlanRideLine({
      ride_id: "btm-uuid",
      ride_name: "Big Thunder",
      predicted_wait_min: 25,
      current_wait: null,
      status: "DOWN",
      held_ll: null,
    });
    expect(line).toContain("[btm-uuid]");
    expect(line).toContain("now DOWN");
  });

  it("plan ride lines show wait + held-LL context", () => {
    const line = formatPlanRideLine({
      ride_id: "hm",
      ride_name: "Haunted Mansion",
      predicted_wait_min: 30,
      current_wait: 20,
      status: "OPERATING",
      held_ll: "2026-07-03T16:00:00-04:00",
    });
    expect(line).toContain("[hm]");
    expect(line).toContain("now 20");
    expect(line).toContain("planned ~30m");
    expect(line).toContain("HELD LL");
  });

  it("handles the all-done day: no remaining rides, full completed list", () => {
    const allDone = {
      ...plan,
      dropped_ride_ids: [],
      completed_ride_ids: ["space", "tron", "btm", "tiana"],
    };
    const { rides, completed_names } = buildReplanModelInput(allDone, live);
    expect(rides).toEqual([]);
    expect(completed_names).toHaveLength(4);
  });
});
