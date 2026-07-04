import { describe, expect, it } from "vitest";

import { pickNextLl } from "./next-ll";

const NOW = new Date("2026-07-04T14:00:00-04:00");
const T = (h: string) => `2026-07-04T${h}:00-04:00`;

const rides = [
  { ride_id: "tt", ride_name: "Test Track" },
  { ride_id: "fz", ride_name: "Frozen Ever After" },
  { ride_id: "gf", ride_name: "Gran Fiesta" },
];

const live = (over: Record<string, { return_start?: string; price?: string } | null>, waits: Record<string, number | null> = {}) =>
  rides.map((r) => ({
    ride_id: r.ride_id,
    wait_mins: waits[r.ride_id] ?? null,
    ll: over[r.ride_id] ?? null,
  }));

describe("pickNextLl (web twin of poller nudge.pick_ll_candidate)", () => {
  it("earliest future return among un-held rides wins", () => {
    const pick = pickNextLl({
      rides,
      holds: {},
      live: live(
        { tt: { return_start: T("16:00"), price: "$12" }, fz: { return_start: T("15:00") } },
        { fz: 40 },
      ),
      now: NOW,
    });
    expect(pick?.ride_id).toBe("fz");
    expect(pick?.standby_mins).toBe(40);
  });

  it("held rides are skipped even with a great offer", () => {
    const pick = pickNextLl({
      rides,
      holds: { fz: T("20:00") },
      live: live({ fz: { return_start: T("14:30") }, tt: { return_start: T("16:00") } }),
      now: NOW,
    });
    expect(pick?.ride_id).toBe("tt");
  });

  it("past returns are unusable; no offers → null", () => {
    expect(
      pickNextLl({
        rides,
        holds: {},
        live: live({ tt: { return_start: T("13:00") } }),
        now: NOW,
      }),
    ).toBeNull();
    expect(pickNextLl({ rides, holds: {}, live: live({}), now: NOW })).toBeNull();
  });

  it("offer without return_start (sold out / null ll) is skipped", () => {
    const pick = pickNextLl({
      rides,
      holds: {},
      live: live({ tt: {}, fz: { return_start: T("17:00"), price: "$15" } }),
      now: NOW,
    });
    expect(pick?.ride_id).toBe("fz");
    expect(pick?.price).toBe("$15");
  });
});
