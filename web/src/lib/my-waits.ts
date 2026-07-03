/**
 * The per-user "my waits" read model — favorites across all parks joined
 * with live waits, plus today's active plan (remaining rides). ONE shape
 * shared by the /waits page and the widget JSON feed, so the phone widget
 * can never drift from what the page shows.
 *
 * Read cost: one favorites Query + one STATE GSI Query per park the user
 * has favorites in (≤4), plus the trips read when a plan might be active.
 * All keyed reads — no scans (DATA-GROWTH-MODEL.md).
 */
import "server-only";

import { getParkRides, getUpcomingTrips, type RideState } from "./dynamodb";
import { getUserFavoriteRides, getFavoriteRideCountsByPark } from "./dynamodb-writes";
import { PARKS, findPark, type ParkKey } from "./parks";

export interface MyWaitRide {
  ride_id: string;
  ride_name: string;
  status: string;
  wait_mins: number | null;
}

export interface MyWaitsParkGroup {
  park_key: ParkKey;
  park_name: string;
  rides: MyWaitRide[];
}

export interface MyWaits {
  /** Today's ACTIVE plan (remaining rides w/ live waits), or null. */
  plan: {
    park_key: ParkKey;
    park_name: string;
    plan_id: string;
    rides: MyWaitRide[];
  } | null;
  /** Only parks where the user has favorites, in canonical park order. */
  parks: MyWaitsParkGroup[];
  /** Freshest STATE row seen — "as of" for the glance. */
  updated_at: string | null;
}

function todayEtIso(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

/** DOWN first (that's the news), then longest wait — glance order. */
function glanceSort(a: MyWaitRide, b: MyWaitRide): number {
  const downA = a.status === "DOWN" ? 1 : 0;
  const downB = b.status === "DOWN" ? 1 : 0;
  if (downA !== downB) return downB - downA;
  return (b.wait_mins ?? -1) - (a.wait_mins ?? -1);
}

function toWaitRide(r: RideState): MyWaitRide {
  return {
    ride_id: r.ride_id,
    ride_name: r.name,
    status: r.status,
    wait_mins: r.wait_mins,
  };
}

export async function getMyWaits(sub: string): Promise<MyWaits> {
  const counts = await getFavoriteRideCountsByPark(sub);
  const favParks = PARKS.filter((p) => (counts[p.key] ?? 0) > 0).map(
    (p) => p.key,
  );

  // Today's active plan (if any) — cheap check, and it decides whether we
  // need a park's STATE rows beyond the favorites set.
  const today = todayEtIso();
  const trips = await getUpcomingTrips();
  const planDay =
    trips
      .flatMap((t) => t.days)
      .find(
        (d) => d.date === today && d.active && !d.outcome_recorded,
      ) ?? null;

  const parksToFetch = new Set<ParkKey>(favParks);
  if (planDay) parksToFetch.add(planDay.park_key);

  const stateByPark = new Map<ParkKey, RideState[]>();
  await Promise.all(
    [...parksToFetch].map(async (pk) => {
      stateByPark.set(pk, await getParkRides(pk));
    }),
  );

  // Favorites per park, joined with live state.
  const groups: MyWaitsParkGroup[] = [];
  for (const pk of favParks) {
    const favIds = await getUserFavoriteRides(sub, pk);
    const rides = (stateByPark.get(pk) ?? [])
      .filter((r) => favIds.has(r.ride_id))
      .map(toWaitRide)
      .sort(glanceSort);
    if (rides.length > 0) {
      groups.push({
        park_key: pk,
        park_name: findPark(pk)?.name ?? pk,
        rides,
      });
    }
  }

  // Plan section: remaining (un-ridden) sequence in PLAN ORDER — the
  // sequence is the plan; re-sorting it would hide what's next.
  let plan: MyWaits["plan"] = null;
  if (planDay) {
    const state = stateByPark.get(planDay.park_key) ?? [];
    const byId = new Map(state.map((r) => [r.ride_id, r]));
    const byName = new Map(state.map((r) => [r.name.toLowerCase(), r]));
    const rides = planDay.rides.map((pr) => {
      const match =
        (pr.ride_id && byId.get(pr.ride_id)) ||
        byName.get(pr.ride_name.toLowerCase());
      return match
        ? toWaitRide(match)
        : { ride_id: pr.ride_id ?? "", ride_name: pr.ride_name, status: "UNKNOWN", wait_mins: null };
    });
    plan = {
      park_key: planDay.park_key,
      park_name: findPark(planDay.park_key)?.name ?? planDay.park_key,
      plan_id: planDay.plan_id,
      rides,
    };
  }

  const allSeen = [...stateByPark.values()].flat().map((r) => r.last_seen);
  return {
    plan,
    parks: groups,
    updated_at: allSeen.length ? allSeen.sort().at(-1)! : null,
  };
}
