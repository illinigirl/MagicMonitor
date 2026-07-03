// Tests for the /waits + widget read model: favorites joined with live
// state (glance-sorted), today's active plan in PLAN order, updated_at.
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

const getParkRides = vi.fn();
const getUpcomingTrips = vi.fn();
vi.mock("./dynamodb", () => ({
  getParkRides: (...a: unknown[]) => getParkRides(...a),
  getUpcomingTrips: (...a: unknown[]) => getUpcomingTrips(...a),
}));

const getUserFavoriteRides = vi.fn();
const getFavoriteRideCountsByPark = vi.fn();
vi.mock("./dynamodb-writes", () => ({
  getUserFavoriteRides: (...a: unknown[]) => getUserFavoriteRides(...a),
  getFavoriteRideCountsByPark: (...a: unknown[]) =>
    getFavoriteRideCountsByPark(...a),
}));

function ride(id: string, name: string, wait: number | null, status = "OPERATING") {
  return {
    ride_id: id, name, status, wait_mins: wait,
    park_key: "magic_kingdom", park_name: "Magic Kingdom",
    last_seen: `2026-07-03T12:0${id.length}:00Z`, ll: null,
  };
}

const NO_COUNTS = {
  magic_kingdom: 0, epcot: 0, hollywood_studios: 0, animal_kingdom: 0,
};

beforeEach(() => {
  vi.resetAllMocks();
  getUpcomingTrips.mockResolvedValue([]);
});

describe("getMyWaits", () => {
  it("groups favorites by park, DOWN first then longest wait", async () => {
    getFavoriteRideCountsByPark.mockResolvedValue({ ...NO_COUNTS, magic_kingdom: 3 });
    getUserFavoriteRides.mockResolvedValue(new Set(["a", "b", "c"]));
    getParkRides.mockResolvedValue([
      ride("a", "Short Wait", 10),
      ride("b", "Broken Ride", null, "DOWN"),
      ride("c", "Long Wait", 60),
      ride("x", "Not A Favorite", 90),
    ]);
    const { getMyWaits } = await import("./my-waits");
    const out = await getMyWaits("sub-1");

    expect(out.parks).toHaveLength(1);
    expect(out.parks[0].rides.map((r) => r.ride_name)).toEqual([
      "Broken Ride", "Long Wait", "Short Wait",
    ]);
    expect(out.plan).toBeNull();
    expect(out.updated_at).not.toBeNull();
  });

  it("surfaces today's active plan in plan order with joined waits", async () => {
    const today = new Date().toLocaleDateString("en-CA", {
      timeZone: "America/New_York",
    });
    getFavoriteRideCountsByPark.mockResolvedValue({ ...NO_COUNTS });
    getUpcomingTrips.mockResolvedValue([{
      trip_id: "t1", name: "Trip", start_date: today, end_date: today,
      days: [{
        date: today, park_key: "epcot", plan_id: "p1", active: true,
        ride_count: 2, outcome_recorded: false, alert_subscribers: [],
        rides: [
          { ride_name: "Second By Plan", ride_id: "s2" },
          { ride_name: "First By Wait", ride_id: "s1" },
        ],
      }],
    }]);
    getParkRides.mockResolvedValue([
      { ...ride("s1", "First By Wait", 70), park_key: "epcot" },
      { ...ride("s2", "Second By Plan", 5), park_key: "epcot" },
    ]);
    const { getMyWaits } = await import("./my-waits");
    const out = await getMyWaits("sub-1");

    // PLAN order preserved (never re-sorted by wait) + waits joined.
    expect(out.plan?.rides.map((r) => [r.ride_name, r.wait_mins])).toEqual([
      ["Second By Plan", 5],
      ["First By Wait", 70],
    ]);
    expect(out.parks).toEqual([]); // no favorites → no park groups
  });

  it("dormant or recorded plans don't appear", async () => {
    const today = new Date().toLocaleDateString("en-CA", {
      timeZone: "America/New_York",
    });
    getFavoriteRideCountsByPark.mockResolvedValue({ ...NO_COUNTS });
    getUpcomingTrips.mockResolvedValue([{
      trip_id: "t1", name: "Trip", start_date: today, end_date: today,
      days: [{
        date: today, park_key: "epcot", plan_id: "p1", active: false,
        ride_count: 1, outcome_recorded: false, alert_subscribers: [],
        rides: [{ ride_name: "X", ride_id: "x" }],
      }],
    }]);
    const { getMyWaits } = await import("./my-waits");
    const out = await getMyWaits("sub-1");
    expect(out.plan).toBeNull();
  });
});
