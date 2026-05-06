/**
 * Showtimes data layer for the M4 "Today at the park" page.
 *
 * Pulled from themeparks.wiki's /live endpoint, which returns SHOW
 * entities alongside ATTRACTION and RESTAURANT. Each SHOW carries a
 * `showtimes[]` array of per-performance start/end ISO datetimes for
 * roughly the next ~24-48h. We filter to today (in park time), drop
 * empty entries (shows listed but not running today), and bucket by
 * a name-based heuristic into headliners vs. atmosphere/character
 * meets.
 *
 * No Lambda or DDB involvement — shows don't trigger alerts and the
 * 10-min cache is plenty fresh for what is fundamentally a daily
 * schedule. Symmetric with `schedule.ts` (park hours).
 *
 * Pure types and helpers (classifier, formatters) live in
 * `showtimes.ts` so the client component can import them. Only the
 * fetcher needs `server-only` protection.
 */
import "server-only";

import type { ParkKey } from "./parks";
import {
  classifyShow,
  HEADLINER_CATEGORIES,
  nextUpcomingTime,
  type ParkShowtimes,
  type ShowEntity,
} from "./showtimes";

const PARK_IDS: Record<ParkKey, string> = {
  magic_kingdom:     "75ea578a-adc8-4116-a54d-dccb60765ef9",
  epcot:             "47f90d2c-e191-4239-a466-5892ef59a88b",
  hollywood_studios: "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
  animal_kingdom:    "1c84a229-8862-4648-9c71-378ddd2c7693",
};

interface RawShow {
  id: string;
  name: string;
  entityType: string;
  status?: string;
  showtimes?: { type?: string; startTime: string; endTime: string }[];
}

/**
 * Today's date in the parks' tz (America/New_York). Used to filter
 * the multi-day showtime array down to today without UTC-midnight
 * rollover bugs.
 */
function todayInPark(): string {
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "America/New_York",
  }).format(new Date());
}

export async function getParkShowtimes(parkKey: ParkKey): Promise<ParkShowtimes | null> {
  const parkId = PARK_IDS[parkKey];
  if (!parkId) return null;

  const url = `https://api.themeparks.wiki/v1/entity/${parkId}/live`;
  let raw: { liveData?: RawShow[] };
  try {
    // 10-min cache — same cadence as schedule.ts. Showtimes for the
    // current day rarely change after they're published.
    const resp = await fetch(url, { next: { revalidate: 600 } });
    if (!resp.ok) return null;
    raw = await resp.json();
  } catch {
    return null;
  }

  const today = todayInPark();
  const shows: ShowEntity[] = [];

  for (const item of raw.liveData ?? []) {
    if (item.entityType !== "SHOW") continue;

    const todays = (item.showtimes ?? [])
      .filter((s) => s.startTime.slice(0, 10) === today)
      .map((s) => ({ start: s.startTime, end: s.endTime }))
      .sort((a, b) => a.start.localeCompare(b.start));

    // Skip shows the API knows about but isn't running today
    // (multi-day festival entries, after-hours-only spectaculars
    // when there's no after-hours event, etc.).
    if (todays.length === 0) continue;

    shows.push({
      id: item.id,
      name: item.name,
      category: classifyShow(item.name),
      showtimes: todays,
    });
  }

  // Sort each bucket by next-upcoming start time so the page reads
  // "what's happening soon" naturally. Shows whose performances are
  // all in the past sort to the bottom.
  const now = new Date();
  const compareByNext = (a: ShowEntity, b: ShowEntity): number => {
    const an = nextUpcomingTime(a, now);
    const bn = nextUpcomingTime(b, now);
    if (an && bn) return an.start.localeCompare(bn.start);
    if (an) return -1;
    if (bn) return 1;
    return a.name.localeCompare(b.name);
  };

  const headliners = shows
    .filter((s) => HEADLINER_CATEGORIES.includes(s.category))
    .sort(compareByNext);
  const more = shows
    .filter((s) => !HEADLINER_CATEGORIES.includes(s.category))
    .sort(compareByNext);

  // "Next up" — soonest unstarted performance anywhere in the park.
  // Spans both buckets so a Voices-of-Liberty set in 5 minutes beats
  // a Happily-Ever-After at 9 PM.
  let nextUp: ParkShowtimes["nextUp"] = null;
  for (const show of [...headliners, ...more]) {
    const nt = nextUpcomingTime(show, now);
    if (!nt) continue;
    if (!nextUp || nt.start.localeCompare(nextUp.time.start) < 0) {
      nextUp = { show, time: nt };
    }
  }

  return { headliners, more, nextUp };
}
