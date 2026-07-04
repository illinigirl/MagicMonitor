// Unit tests for getParkRides() — the read path that powers each
// park's dashboard page.
//
// History: this function was the site of the 2026-05-24 silent
// regression where a single-page DDB Scan stopped returning STATE
// rows once WAIT# rows pushed past page 1. The immediate fix
// added pagination; the structural fix added a GSI on park_key
// and switched Scan → Query. These tests are the test-time layer
// of the three-layer defense documented in TESTING.md "Silent
// regressions from data growth": they exercise the pagination
// loop and pin the GSI name/key-condition contract so future
// drifts get caught at PR time, not in production.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `server-only` throws at import time outside server contexts. Mock
// it to a no-op so the module-under-test can load cleanly in the
// Vitest node environment.
vi.mock("server-only", () => ({}));

// Capture the `send` calls so each test can stub responses and
// assert the right command was issued.
const sendMock = vi.fn();

// Replace the DDB DocumentClient with one whose `send` is our spy.
// The `client = ... DynamoDBDocumentClient.from(...)` line in
// dynamodb.ts becomes our mock client, and every `client.send(cmd)`
// call lands on sendMock.
vi.mock("@aws-sdk/lib-dynamodb", async () => {
  const actual = await vi.importActual<typeof import("@aws-sdk/lib-dynamodb")>(
    "@aws-sdk/lib-dynamodb",
  );
  return {
    ...actual,
    DynamoDBDocumentClient: {
      ...actual.DynamoDBDocumentClient,
      from: () => ({ send: sendMock }),
    },
  };
});

beforeEach(() => {
  sendMock.mockReset();
  // The dynamodb.ts module caches the client on globalThis in non-
  // production environments. Reset between tests so each test gets
  // a freshly constructed (mocked) client.
  delete (globalThis as { __ddbClient?: unknown }).__ddbClient;
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeStateItem(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ride_id: "ride-abc",
    park_key: "magic_kingdom",
    park_name: "Magic Kingdom",
    name: "Test Ride",
    status: "OPERATING",
    wait_mins: 15,
    last_seen: "2026-05-25T12:00:00Z",
    ll: null,
    ...overrides,
  };
}

describe("getParkRides", () => {
  it("returns items from a single-page response, sorted by name", async () => {
    sendMock.mockResolvedValueOnce({
      Items: [
        makeStateItem({ name: "Tomorrowland Speedway", ride_id: "r2" }),
        makeStateItem({ name: "Astro Orbiter", ride_id: "r1" }),
      ],
      LastEvaluatedKey: undefined,
    });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("magic_kingdom");

    expect(items).toHaveLength(2);
    expect(items[0].name).toBe("Astro Orbiter");
    expect(items[1].name).toBe("Tomorrowland Speedway");
    expect(sendMock).toHaveBeenCalledTimes(1);
  });

  it("accumulates across paginated responses (defense for the 2026-05-24 regression category)", async () => {
    // Simulate a partition that's outgrown a single 1MB page: first
    // page returns some items + a LastEvaluatedKey; second page
    // returns the rest + no LastEvaluatedKey. The function must
    // walk all pages, otherwise we re-introduce the silent-empty
    // bug that lived in production for ~7 days.
    sendMock
      .mockResolvedValueOnce({
        Items: [makeStateItem({ name: "Big Thunder Mountain", ride_id: "r1" })],
        LastEvaluatedKey: { PK: "p1", SK: "STATE" },
      })
      .mockResolvedValueOnce({
        Items: [makeStateItem({ name: "Astro Orbiter", ride_id: "r2" })],
        LastEvaluatedKey: undefined,
      });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("magic_kingdom");

    expect(items).toHaveLength(2);
    // Sorted by name: Astro before Big.
    expect(items.map((r) => r.name)).toEqual([
      "Astro Orbiter",
      "Big Thunder Mountain",
    ]);
    expect(sendMock).toHaveBeenCalledTimes(2);

    // Second call must have passed the LastEvaluatedKey from the
    // first response — that's the actual pagination contract.
    const secondCallCommand = sendMock.mock.calls[1][0];
    expect(secondCallCommand.input.ExclusiveStartKey).toEqual({
      PK: "p1",
      SK: "STATE",
    });
  });

  it("queries the park_key-SK GSI with the correct key condition", async () => {
    // Pins the GSI contract: index name, key-condition shape, and
    // expression-attribute values. Catches accidental regressions
    // back to a Scan or a typo in the index name. The GSI was added
    // in the 2026-05-25 CDK deploy; this test ensures the reader
    // continues to use it.
    sendMock.mockResolvedValueOnce({ Items: [], LastEvaluatedKey: undefined });

    const { getParkRides } = await import("./dynamodb");
    await getParkRides("epcot");

    const sentCommand = sendMock.mock.calls[0][0];
    expect(sentCommand.input.IndexName).toBe("park_key-SK-index");
    expect(sentCommand.input.KeyConditionExpression).toBe(
      "park_key = :p AND SK = :sk",
    );
    expect(sentCommand.input.ExpressionAttributeValues).toEqual({
      ":sk": "STATE",
      ":p": "epcot",
    });
  });

  it("returns empty array when partition has no STATE rows", async () => {
    sendMock.mockResolvedValueOnce({ Items: [], LastEvaluatedKey: undefined });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("hollywood_studios");

    expect(items).toEqual([]);
  });
});

// ── getUpcomingTrips() — the /trips read path ──────────────────────
//
// Fires two paginated Queries (TRIP# headers + PLAN# rows) against the
// shared USER#megan partition, then DERIVES each trip's days + date
// range from the PLAN# rows (the (Y) model — the header's denormalized
// `days` is deliberately ignored so it can't drift). These pin that
// derivation, the upcoming/past cutoff, the partition contract, and the
// pagination loop (same data-growth defense as getParkRides).

function makeTripRow(over: Partial<Record<string, unknown>> = {}) {
  return { SK: "TRIP#t1", name: "June trip", ...over };
}
function makePlanRow(over: Partial<Record<string, unknown>> = {}) {
  return {
    SK: "PLAN#p1",
    trip_id: "t1",
    planned_for_date: "2099-09-01",
    park_key: "magic_kingdom",
    active: false,
    outcome_recorded: false,
    ride_sequence: [],
    ...over,
  };
}

// Route the two interleaved Queries by their SK prefix so the test is
// independent of Promise.all call ordering. planPages feeds successive
// PLAN# calls (for the pagination test).
function mockTripsAndPlans(
  tripItems: unknown[],
  planPages: { Items: unknown[]; LastEvaluatedKey?: unknown }[],
) {
  let planCall = 0;
  sendMock.mockImplementation((cmd: { input: { ExpressionAttributeValues: Record<string, unknown> } }) => {
    const sk = cmd.input.ExpressionAttributeValues[":sk"];
    if (sk === "TRIP#") return Promise.resolve({ Items: tripItems, LastEvaluatedKey: undefined });
    if (sk === "PLAN#") {
      const page = planPages[planCall] ?? { Items: [], LastEvaluatedKey: undefined };
      planCall += 1;
      return Promise.resolve(page);
    }
    return Promise.resolve({ Items: [], LastEvaluatedKey: undefined });
  });
}

describe("getUpcomingTrips", () => {
  it("derives days + date range from PLAN# rows, sorted by date", async () => {
    mockTripsAndPlans(
      [makeTripRow({ name: "June trip" })],
      [{
        Items: [
          // intentionally out of date order to prove we sort
          makePlanRow({ SK: "PLAN#p2", planned_for_date: "2099-09-02", park_key: "epcot",
                        ride_sequence: [{ ride_name: "Test Track" }] }),
          makePlanRow({ SK: "PLAN#p1", planned_for_date: "2099-09-01", park_key: "magic_kingdom",
                        active: true,
                        ride_sequence: [{ ride_name: "Space", ride_id: "sm" }, { ride_name: "TRON" }] }),
        ],
        LastEvaluatedKey: undefined,
      }],
    );

    const { getUpcomingTrips } = await import("./dynamodb");
    const trips = await getUpcomingTrips();

    expect(trips).toHaveLength(1);
    const t = trips[0];
    expect(t.name).toBe("June trip");
    expect(t.start_date).toBe("2099-09-01");   // derived from rows, min
    expect(t.end_date).toBe("2099-09-02");     // derived from rows, max
    expect(t.days.map((d) => d.date)).toEqual(["2099-09-01", "2099-09-02"]);
    expect(t.days[0].park_key).toBe("magic_kingdom");
    expect(t.days[0].active).toBe(true);
    expect(t.days[0].ride_count).toBe(2);
    expect(t.days[0].rides.map((r) => r.ride_name)).toEqual(["Space", "TRON"]);
    expect(t.days[0].plan_id).toBe("p1");
  });

  it("skips a trip whose latest day is in the past", async () => {
    mockTripsAndPlans(
      [makeTripRow()],
      [{ Items: [makePlanRow({ planned_for_date: "2000-01-01" })], LastEvaluatedKey: undefined }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    expect(await getUpcomingTrips()).toEqual([]);
  });

  it("skips a header with no PLAN# day rows", async () => {
    mockTripsAndPlans([makeTripRow({ SK: "TRIP#ghost" })], [{ Items: [], LastEvaluatedKey: undefined }]);
    const { getUpcomingTrips } = await import("./dynamodb");
    expect(await getUpcomingTrips()).toEqual([]);
  });

  it("paginates the PLAN# query (data-growth defense)", async () => {
    mockTripsAndPlans(
      [makeTripRow()],
      [
        { Items: [makePlanRow({ SK: "PLAN#p1", planned_for_date: "2099-09-01" })],
          LastEvaluatedKey: { PK: "USER#megan", SK: "PLAN#p1" } },
        { Items: [makePlanRow({ SK: "PLAN#p2", planned_for_date: "2099-09-02" })],
          LastEvaluatedKey: undefined },
      ],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    const trips = await getUpcomingTrips();
    expect(trips[0].days.map((d) => d.date)).toEqual(["2099-09-01", "2099-09-02"]);
  });

  it("collapses duplicate same-date rows to one day, preferring the active row", async () => {
    // Legacy data can hold two PLAN# rows for the same (trip, date) —
    // the pre-upsert record_plan appended instead of updating. The
    // reader must render that date ONCE, preferring the active row (and
    // its richer ride list) so a stale dormant dup never doubles a day.
    mockTripsAndPlans(
      [makeTripRow()],
      [{
        Items: [
          makePlanRow({ SK: "PLAN#2099-01-01T10:00:00+00:00", planned_for_date: "2099-09-01",
                        active: false, ride_sequence: [{ ride_name: "A" }] }),
          makePlanRow({ SK: "PLAN#2099-01-01T11:00:00+00:00", planned_for_date: "2099-09-01",
                        active: true, ride_sequence: [{ ride_name: "A" }, { ride_name: "B" }] }),
        ],
        LastEvaluatedKey: undefined,
      }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    const trips = await getUpcomingTrips();
    expect(trips).toHaveLength(1);
    expect(trips[0].days).toHaveLength(1);          // the date shows ONCE
    expect(trips[0].days[0].active).toBe(true);     // active row preferred
    expect(trips[0].days[0].ride_count).toBe(2);
  });

  it("queries the shared USER#megan partition with begins_with", async () => {
    mockTripsAndPlans([], [{ Items: [], LastEvaluatedKey: undefined }]);
    const { getUpcomingTrips } = await import("./dynamodb");
    await getUpcomingTrips();
    const cmds = sendMock.mock.calls.map((c) => c[0].input);
    const tripQ = cmds.find((i) => i.ExpressionAttributeValues[":sk"] === "TRIP#");
    expect(tripQ.KeyConditionExpression).toBe("PK = :pk AND begins_with(SK, :sk)");
    expect(tripQ.ExpressionAttributeValues[":pk"]).toBe("USER#megan");
    expect(tripQ.IndexName).toBeUndefined(); // base table, not a GSI
  });

  it("surfaces a standalone plan (no trip_id) as a single-day entry", async () => {
    // A same-day record_plan writes a lone PLAN# row with no trip_id and no
    // TRIP# header. It must still appear on /trips — it lives in the shared
    // partition, so it shows up nowhere else (this was the reported bug).
    mockTripsAndPlans(
      [], // no TRIP# headers at all
      [{
        Items: [
          makePlanRow({
            SK: "PLAN#solo1", trip_id: undefined, planned_for_date: "2099-09-01",
            park_key: "epcot", active: true,
            ride_sequence: [{ ride_name: "Test Track", ride_id: "tt" }],
          }),
        ],
        LastEvaluatedKey: undefined,
      }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    const { findPark } = await import("./parks");
    const trips = await getUpcomingTrips();

    expect(trips).toHaveLength(1);
    const t = trips[0];
    expect(t.trip_id).toBe("solo:2099-09-01");
    expect(t.start_date).toBe("2099-09-01");
    expect(t.end_date).toBe("2099-09-01");
    expect(t.days).toHaveLength(1);
    expect(t.days[0].park_key).toBe("epcot");
    expect(t.days[0].active).toBe(true);
    expect(t.days[0].plan_id).toBe("solo1");
    expect(t.days[0].rides.map((r) => r.ride_name)).toEqual(["Test Track"]);
    // A future standalone day is titled by its park name (not "Today's plan").
    expect(t.name).toBe(findPark("epcot")!.name);
  });

  it("labels a same-day standalone plan \"Today's plan\"", async () => {
    // Freeze the clock mid-day UTC so the ET calendar date is unambiguous
    // (18:00Z → 14:00 ET → 2099-09-01), then plan FOR that same date.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2099-09-01T18:00:00Z"));
    try {
      mockTripsAndPlans(
        [],
        [{
          Items: [
            makePlanRow({
              SK: "PLAN#today1", trip_id: undefined, planned_for_date: "2099-09-01",
              park_key: "magic_kingdom", active: true,
            }),
          ],
          LastEvaluatedKey: undefined,
        }],
      );
      const { getUpcomingTrips } = await import("./dynamodb");
      const trips = await getUpcomingTrips();
      expect(trips).toHaveLength(1);
      expect(trips[0].name).toBe("Today's plan");
      expect(trips[0].days[0].active).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("skips a standalone plan whose date is in the past", async () => {
    mockTripsAndPlans(
      [],
      [{
        Items: [makePlanRow({ SK: "PLAN#old", trip_id: undefined, planned_for_date: "2000-01-01" })],
        LastEvaluatedKey: undefined,
      }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    expect(await getUpcomingTrips()).toEqual([]);
  });

  it("shows a standalone plan alongside a header-grouped trip, sorted by date", async () => {
    mockTripsAndPlans(
      [makeTripRow({ SK: "TRIP#t1", name: "June trip" })],
      [{
        Items: [
          makePlanRow({ SK: "PLAN#p1", trip_id: "t1", planned_for_date: "2099-09-10", park_key: "magic_kingdom" }),
          makePlanRow({ SK: "PLAN#solo", trip_id: undefined, planned_for_date: "2099-09-02", park_key: "epcot" }),
        ],
        LastEvaluatedKey: undefined,
      }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    const trips = await getUpcomingTrips();
    expect(trips).toHaveLength(2);
    // Sorted by start_date: the standalone 09-02 before the trip's 09-10.
    expect(trips[0].trip_id).toBe("solo:2099-09-02");
    expect(trips[1].name).toBe("June trip");
  });
});

describe("getUpcomingTrips alert_subscribers", () => {
  it("normalizes the DDB String Set to a sorted array (absent → [])", async () => {
    mockTripsAndPlans(
      [makeTripRow()],
      [{
        Items: [
          makePlanRow({ SK: "PLAN#p1", planned_for_date: "2099-09-01",
                        alert_subscribers: new Set(["sub-b", "sub-a"]) }),
          makePlanRow({ SK: "PLAN#p2", planned_for_date: "2099-09-02" }),
        ],
        LastEvaluatedKey: undefined,
      }],
    );
    const { getUpcomingTrips } = await import("./dynamodb");
    const trips = await getUpcomingTrips();
    expect(trips[0].days[0].alert_subscribers).toEqual(["sub-a", "sub-b"]);
    expect(trips[0].days[1].alert_subscribers).toEqual([]);
  });
});

describe("getMemberNames", () => {
  it("resolves subs to profile names, dedupes, and falls back on missing", async () => {
    // One GetItem per unique sub; second call returns no profile.
    sendMock
      .mockResolvedValueOnce({ Item: { name: "Michele" } })
      .mockResolvedValueOnce({ Item: undefined });
    const { getMemberNames } = await import("./dynamodb");
    const names = await getMemberNames([
      "01db7540-aaaa",
      "01db7540-aaaa", // duplicate → one lookup
      "beef1234-bbbb",
    ]);
    expect(sendMock).toHaveBeenCalledTimes(2); // deduped
    expect(names["01db7540-aaaa"]).toBe("Michele");
    // Missing profile → short-id fallback, never blank.
    expect(names["beef1234-bbbb"]).toBe("beef12…");
  });
});

describe("orderedDayRides", () => {
  // Pins the /trips fix from 2026-07-03: the page rendered raw
  // ride_sequence, so an APPLIED re-plan still displayed the original
  // order with dropped rides present ("did the replan not persist?").
  const row = {
    ride_sequence: [
      { ride_name: "Space Mountain", ride_id: "space" },
      { ride_name: "Big Thunder", ride_id: "btm" },
      { ride_name: "Tiana's Bayou", ride_id: "tiana" },
      { ride_name: "PhilharMagic", ride_id: "phil" }, // added via replan
    ],
    plan_order: ["btm", "phil"],
    dropped_ride_ids: new Set(["tiana"]),
    completed_ride_ids: new Set(["space"]),
  };

  it("honors plan_order, excludes dropped, flags done", async () => {
    const { orderedDayRides } = await import("./dynamodb");
    const rides = orderedDayRides(row);
    expect(rides.map((r) => r.ride_id)).toEqual(["btm", "phil", "space"]);
    expect(rides.find((r) => r.ride_id === "space")?.done).toBe(true);
    expect(rides.find((r) => r.ride_id === "btm")?.done).toBe(false);
  });

  it("no plan_order → original sequence order, drops still excluded", async () => {
    const { orderedDayRides } = await import("./dynamodb");
    const rides = orderedDayRides({ ...row, plan_order: undefined });
    expect(rides.map((r) => r.ride_id)).toEqual(["space", "btm", "phil"]);
  });

  it("handles DDB Sets or arrays for the id sets", async () => {
    const { orderedDayRides } = await import("./dynamodb");
    const rides = orderedDayRides({
      ...row,
      dropped_ride_ids: ["tiana"],
      completed_ride_ids: ["space"],
    });
    expect(rides.map((r) => r.ride_id)).toEqual(["btm", "phil", "space"]);
  });
});

describe("orderedDayRides held-LL passthrough", () => {
  // Pins the 2026-07-04 trip-page gap: holds existed in the row but the
  // day cards had no way to show them.
  it("carries held-LL return times through, null when unheld", async () => {
    const { orderedDayRides } = await import("./dynamodb");
    const rides = orderedDayRides({
      ride_sequence: [
        { ride_name: "Big Thunder", ride_id: "btm" },
        { ride_name: "PhilharMagic", ride_id: "phil" },
      ],
      ll_holds: { btm: "2026-07-04T14:30:00-04:00" },
    });
    expect(rides.find((r) => r.ride_id === "btm")?.held_ll).toBe(
      "2026-07-04T14:30:00-04:00",
    );
    expect(rides.find((r) => r.ride_id === "phil")?.held_ll).toBeNull();
  });
});
