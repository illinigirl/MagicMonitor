/**
 * Park-schedule fetching from themeparks.wiki.
 *
 * The /schedule endpoint returns a multi-day array with several entry
 * types per day. We care about three:
 *
 *   - OPERATING                    — regular park hours
 *   - TICKETED_EVENT (Early Entry) — 30-min head start for on-site guests
 *   - EXTRA_HOURS                  — Extended Evening Hours (deluxe + DVC only)
 *
 * Other TICKETED_EVENT types exist (Disney After Hours, Halloween parties)
 * but we treat those as third-party events the regular dashboard shouldn't
 * surface — they require separate ticketing.
 */
import "server-only";

import type { ParkKey } from "./parks";

const PARK_IDS: Record<ParkKey, string> = {
  magic_kingdom:     "75ea578a-adc8-4116-a54d-dccb60765ef9",
  epcot:             "47f90d2c-e191-4239-a466-5892ef59a88b",
  hollywood_studios: "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
  animal_kingdom:    "1c84a229-8862-4648-9c71-378ddd2c7693",
};

export interface ScheduleSegment {
  /** ISO datetime with TZ offset, e.g. "2026-05-04T09:00:00-04:00" */
  open: string;
  close: string;
  kind: "operating" | "early_entry" | "extended_evening";
}

export interface ParkSchedule {
  today: ScheduleSegment[];
  tomorrow: ScheduleSegment[];
  /** Convenience: today's main OPERATING segment if there is one */
  todayOperating?: ScheduleSegment;
  /** Convenience: today's Extended Evening Hours if any (deluxe-only) */
  todayExtraHours?: ScheduleSegment;
  /** Convenience: today's Early Entry if any (resort-guest-only) */
  todayEarlyEntry?: ScheduleSegment;
  /** Convenience: tomorrow's main OPERATING segment, for "opens at" lines */
  tomorrowOperating?: ScheduleSegment;
}

interface RawScheduleEntry {
  date: string;
  type: "OPERATING" | "TICKETED_EVENT" | "EXTRA_HOURS" | string;
  description?: string;
  openingTime: string;
  closingTime: string;
}

/**
 * Today's date in Eastern time (Disney World's tz). Used to filter the
 * multi-day schedule down to today + tomorrow without timezone confusion
 * around the UTC midnight rollover.
 */
function todayDates(): { today: string; tomorrow: string } {
  // sv-SE locale gives YYYY-MM-DD format directly, no parsing needed.
  const fmt = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "America/New_York",
  });
  const now = new Date();
  const tomorrow = new Date(now.getTime() + 24 * 60 * 60 * 1000);
  return { today: fmt.format(now), tomorrow: fmt.format(tomorrow) };
}

function classify(entry: RawScheduleEntry): ScheduleSegment["kind"] | null {
  if (entry.type === "OPERATING") return "operating";
  if (entry.type === "EXTRA_HOURS") return "extended_evening";
  if (
    entry.type === "TICKETED_EVENT" &&
    entry.description?.toLowerCase() === "early entry"
  ) {
    return "early_entry";
  }
  return null; // skip parties / after-hours events
}

export async function getParkSchedule(parkKey: ParkKey): Promise<ParkSchedule | null> {
  const parkId = PARK_IDS[parkKey];
  if (!parkId) return null;

  const url = `https://api.themeparks.wiki/v1/entity/${parkId}/schedule`;
  let raw: { schedule?: RawScheduleEntry[] };
  try {
    const resp = await fetch(url, { next: { revalidate: 600 } }); // cache 10 min
    if (!resp.ok) return null;
    raw = await resp.json();
  } catch {
    return null;
  }

  const { today, tomorrow } = todayDates();

  const collect = (date: string): ScheduleSegment[] => {
    const segments: ScheduleSegment[] = [];
    for (const entry of raw.schedule ?? []) {
      if (entry.date !== date) continue;
      const kind = classify(entry);
      if (!kind) continue;
      segments.push({
        open: entry.openingTime,
        close: entry.closingTime,
        kind,
      });
    }
    // Sort by open time so the earliest segment (usually Early Entry)
    // comes first when we render the strip.
    return segments.sort((a, b) => a.open.localeCompare(b.open));
  };

  const todaySegments = collect(today);
  const tomorrowSegments = collect(tomorrow);

  return {
    today: todaySegments,
    tomorrow: tomorrowSegments,
    todayOperating: todaySegments.find((s) => s.kind === "operating"),
    todayExtraHours: todaySegments.find((s) => s.kind === "extended_evening"),
    todayEarlyEntry: todaySegments.find((s) => s.kind === "early_entry"),
    tomorrowOperating: tomorrowSegments.find((s) => s.kind === "operating"),
  };
}

/**
 * Is the park currently open to the general public (regular operating
 * hours, not in a closed window or in a deluxe-only EEH window)?
 *
 * Returns false if the park is in EEH but regular operating ended —
 * the page should still show "Closed (Extended Evening for deluxe)"
 * in that case so a guest without the perk doesn't drive over.
 */
export function isParkOpenNow(schedule: ParkSchedule): boolean {
  if (!schedule.todayOperating) return false;
  const now = new Date();
  const open = new Date(schedule.todayOperating.open);
  const close = new Date(schedule.todayOperating.close);
  return now >= open && now < close;
}

/** Is the park currently in Extended Evening Hours (deluxe / DVC only)? */
export function isExtraHoursNow(schedule: ParkSchedule): boolean {
  if (!schedule.todayExtraHours) return false;
  const now = new Date();
  const open = new Date(schedule.todayExtraHours.open);
  const close = new Date(schedule.todayExtraHours.close);
  return now >= open && now < close;
}

/** Is the park currently in Early Entry (resort-guest only)? */
export function isEarlyEntryNow(schedule: ParkSchedule): boolean {
  if (!schedule.todayEarlyEntry) return false;
  const now = new Date();
  const open = new Date(schedule.todayEarlyEntry.open);
  const close = new Date(schedule.todayEarlyEntry.close);
  return now >= open && now < close;
}

export function formatTimeShort(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  });
}
