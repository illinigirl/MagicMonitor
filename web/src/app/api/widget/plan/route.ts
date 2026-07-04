/**
 * GET /api/widget/plan?t=<sub>.<secret> — today's-plan widget feed.
 *
 * Same capability-token pattern as /api/widget/waits, but against the
 * PLAN-widget secret: the plan is SHARED FAMILY data, and that secret
 * is only ever minted on the family-gated /trips page, so possession
 * proves the holder passed the gate once (see dynamodb-writes.ts).
 *
 * Returns today's day as one merged timeline (rides in plan order with
 * meals/shows slotted by time — the same buildDayTimeline the pages
 * use, so widget and site can't disagree about the day). Times are
 * pre-formatted so the Scriptable script stays dumb.
 */
import { createHash, timingSafeEqual } from "crypto";

import { NextRequest, NextResponse } from "next/server";

import { findTodayDay, getUpcomingTrips, todayEtIso } from "@/lib/dynamodb";
import { getPlanWidgetSecret } from "@/lib/dynamodb-writes";
import { formatEtTime } from "@/lib/format-et";
import { buildDayTimeline } from "@/lib/plan-timeline";
import { findPark } from "@/lib/parks";
import { getCurrentConditions } from "@/lib/weather";

export const dynamic = "force-dynamic";

function digest(s: string): Buffer {
  return createHash("sha256").update(s).digest();
}

export async function GET(req: NextRequest) {
  const token = req.nextUrl.searchParams.get("t") ?? "";
  const dot = token.indexOf(".");
  if (dot <= 0) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const sub = token.slice(0, dot);
  const secret = token.slice(dot + 1);

  const stored = await getPlanWidgetSecret(sub);
  if (!stored || !timingSafeEqual(digest(secret), digest(stored))) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const today = todayEtIso();
  const [trips, weather] = await Promise.all([
    getUpcomingTrips(),
    getCurrentConditions(),
  ]);
  const day = findTodayDay(trips, today);
  if (!day) {
    return NextResponse.json(
      { found: false, date: today, weather },
      { headers: { "cache-control": "no-store" } },
    );
  }

  const timeline = buildDayTimeline(day.rides, day.reservations, day.shows);
  const entries = timeline.map((e) =>
    e.kind === "ride"
      ? {
          kind: "ride" as const,
          name: e.ride.ride_name,
          time: e.ride.target_time ? formatEtTime(e.ride.target_time) : null,
          done: Boolean(e.ride.done),
          held_ll: e.ride.held_ll ? formatEtTime(e.ride.held_ll) : null,
        }
      : {
          kind: e.kind,
          name: e.name,
          time: formatEtTime(e.time),
          booked: e.booked,
        },
  );

  return NextResponse.json(
    {
      found: true,
      date: today,
      park_key: day.park_key,
      park_name: findPark(day.park_key)?.name ?? day.park_key,
      active: day.active,
      plan_id: day.plan_id,
      entries,
      weather,
    },
    { headers: { "cache-control": "no-store" } },
  );
}
