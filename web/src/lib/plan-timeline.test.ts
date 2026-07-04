import { describe, expect, it } from "vitest";

import { buildDayTimeline } from "./plan-timeline";

const T = (h: string) => `2026-07-04T${h}:00-04:00`;

const rides = [
  { ride_name: "Remy", ride_id: "remy", target_time: T("10:00") },
  { ride_name: "Nemo", ride_id: "nemo", target_time: T("11:25") },
  { ride_name: "Spaceship Earth", ride_id: "sse", target_time: T("12:45") },
  { ride_name: "Guardians", ride_id: "gotg", target_time: T("14:30") },
];

function names(entries: ReturnType<typeof buildDayTimeline>) {
  return entries.map((e) => (e.kind === "ride" ? e.ride.ride_name : e.name));
}

describe("buildDayTimeline", () => {
  it("slots meals and shows between rides by time", () => {
    const out = buildDayTimeline(
      rides,
      [
        { name: "Sunshine Seasons", time: T("11:50"), type: "quick-service" },
        { name: "San Angel Inn", time: T("18:00"), type: "dining" },
      ],
      [{ name: "Luminous", start: T("21:00") }],
    );
    expect(names(out)).toEqual([
      "Remy",
      "Nemo",
      "Sunshine Seasons", // 11:50 lands between 11:25 and 12:45
      "Spaceship Earth",
      "Guardians",
      "San Angel Inn", // after the last ride, still time-sorted
      "Luminous",
    ]);
  });

  it("flags quick-service as suggested (booked=false), reservations booked", () => {
    const out = buildDayTimeline(
      rides,
      [
        { name: "Sunshine Seasons", time: T("11:50"), type: "quick-service" },
        { name: "San Angel Inn", time: T("18:00"), type: "dining" },
        { name: "Untyped Place", time: T("13:00") },
      ],
      [],
    );
    const meals = out.flatMap((e) => (e.kind === "meal" ? [e] : []));
    expect(meals.map((m) => [m.name, m.booked])).toEqual([
      ["Sunshine Seasons", false],
      ["Untyped Place", true], // no type → assume a booked reservation
      ["San Angel Inn", true],
    ]);
  });

  it("keeps ride ORDER authoritative — extras never reorder rides", () => {
    // A ride whose target_time is out of order (re-planned day) stays put.
    const odd = [
      { ride_name: "B", ride_id: "b", target_time: T("15:00") },
      { ride_name: "A", ride_id: "a", target_time: T("10:00") },
    ];
    const out = buildDayTimeline(odd, [{ name: "Lunch", time: T("12:00") }], []);
    // Lunch inserts before the first ride LATER than noon in list order (B).
    expect(names(out)).toEqual(["Lunch", "B", "A"]);
  });

  it("un-timed rides never attract insertions; extras go to the end", () => {
    const untimed = [
      { ride_name: "X", ride_id: "x" },
      { ride_name: "Y", ride_id: "y", target_time: null },
    ];
    const out = buildDayTimeline(
      untimed,
      [{ name: "Dinner", time: T("18:00") }],
      [{ name: "Parade", start: T("15:00") }],
    );
    expect(names(out)).toEqual(["X", "Y", "Parade", "Dinner"]);
  });

  it("empty everything → empty timeline", () => {
    expect(buildDayTimeline([], [], [])).toEqual([]);
  });

  it("same-slot extras stay in time order", () => {
    const out = buildDayTimeline(
      rides,
      [
        { name: "Snack", time: T("13:30"), type: "quick-service" },
        { name: "Coffee", time: T("13:00"), type: "quick-service" },
      ],
      [],
    );
    expect(names(out)).toEqual([
      "Remy",
      "Nemo",
      "Spaceship Earth",
      "Coffee",
      "Snack",
      "Guardians",
    ]);
  });
});
