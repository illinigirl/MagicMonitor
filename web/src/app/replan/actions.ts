"use server";

/**
 * Server action for /replan — the one-tap "drop this ride / keep it"
 * approve loop reached from a disruption alert's Pushover deep-link.
 *
 * Family-gated like /trips (the plan is shared). The write is the atomic
 * dropped_ride_ids set op (never touches ride_sequence), so approving a
 * drop can't race with an MCP plan edit. Human-in-the-loop by design:
 * nothing changes until this action runs from a tap.
 */

import { revalidatePath } from "next/cache";

import { auth } from "@/auth";
import { getParkRides, getReplanContext } from "@/lib/dynamodb";
import {
  addRidesToSequence,
  bumpReplanLlmCount,
  setHeldLl,
  setPlanNextUp,
  setPlanOrder,
  setRideActualWait,
  setRideDone,
  setRideDropped,
} from "@/lib/dynamodb-writes";
import {
  buildReplanModelInput,
  proposeReplan,
  splitReplanAdds,
  type ReplanSuggestion,
} from "@/lib/claude-replan";
import { completeRideAndAdvance } from "@/lib/plan-complete";
import { formatEtTime } from "@/lib/format-et";
import { pickNextLl } from "@/lib/next-ll";
import { getCurrentConditions } from "@/lib/weather";
import { isTripsAllowed } from "@/lib/trips-access";

const ASK_CLAUDE_DAILY_CAP = 20;

export interface ReplanResult {
  ok: boolean;
  error?: string;
  /** After a successful Mark done: the hold-aware "next LL worth
   *  grabbing" pick, when one exists (see lib/next-ll.ts). */
  ll_suggestion?: {
    ride_name: string;
    return_label: string;
    price: string | null;
    standby_mins: number | null;
  };
}

async function gate(
  planId: string,
  rideId: string,
): Promise<ReplanResult | null> {
  const session = await auth();
  if (!session?.user?.id) return { ok: false, error: "Not signed in." };
  if (!isTripsAllowed(session.user?.email)) {
    return { ok: false, error: "Family accounts only." };
  }
  if (!planId || !rideId || planId.length > 100 || rideId.length > 100) {
    return { ok: false, error: "Missing plan or ride." };
  }
  return null;
}

export async function applyDrop(
  planId: string,
  rideId: string,
  dropped: boolean,
): Promise<ReplanResult> {
  const bad = await gate(planId, rideId);
  if (bad) return bad;
  try {
    await setRideDropped(planId, rideId, dropped);
  } catch {
    return { ok: false, error: "Couldn't update — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true };
}

export type AskClaudeResult =
  | { ok: true; suggestion: ReplanSuggestion }
  | { ok: false; error: string };

/**
 * "Ask Claude" — a server-side Sonnet call that returns a holistic
 * re-plan suggestion (or "no changes needed") for the day's plan. Costs
 * real tokens, so: family-gated + a per-user daily cap. Tap-only (there's
 * no automatic caller).
 */
export async function askClaudeReplan(
  planId: string,
  trigger?: string | null,
  note?: string | null,
): Promise<AskClaudeResult> {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) return { ok: false, error: "Not signed in." };
  if (!isTripsAllowed(session.user?.email)) {
    return { ok: false, error: "Family accounts only." };
  }

  const today = new Date().toLocaleDateString("en-CA", {
    timeZone: "America/New_York",
  });
  try {
    const count = await bumpReplanLlmCount(sub, today);
    if (count > ASK_CLAUDE_DAILY_CAP) {
      return {
        ok: false,
        error: `Daily limit reached (${ASK_CLAUDE_DAILY_CAP} Ask-Claude checks). Try again tomorrow.`,
      };
    }

    const ctx = await getReplanContext(planId);
    if (!ctx) return { ok: false, error: "Plan not found." };

    const [state, weather] = await Promise.all([
      getParkRides(ctx.park_key),
      getCurrentConditions(),
    ]);
    // buildReplanModelInput is the completed/dropped boundary: rides =
    // genuinely remaining only (the 2026-07-03 bug sent already-ridden
    // rides as "remaining", so Sonnet re-planned a fictional day).
    const { rides, completed_names, catalog } = buildReplanModelInput(ctx, state);

    const suggestion = await proposeReplan({
      park_name: ctx.park_name,
      date: ctx.date,
      weather: weather ? `${weather.condition}, ${weather.temp_f}°` : null,
      trigger: trigger ?? null,
      note: (note ?? "").trim().slice(0, 500) || null,
      rides,
      completed_names,
      catalog,
    });
    return { ok: true, suggestion };
  } catch (err) {
    console.warn("[replan/ask] failed:", err);
    return { ok: false, error: "Couldn't reach Claude — try again." };
  }
}

/**
 * Apply a Claude-suggested re-plan: set the new ride order + drop the
 * rides it flagged. Both are atomic (plan_order SET, dropped_ride_ids
 * ADD). Family-gated; the suggestion itself was already produced behind
 * the daily cap, so this apply is free.
 */
export async function applyReplanOrder(
  planId: string,
  order: string[],
  drop: string[],
  add: { ride_id: string; ride_name: string }[] = [],
): Promise<ReplanResult> {
  const session = await auth();
  if (!session?.user?.id) return { ok: false, error: "Not signed in." };
  if (!isTripsAllowed(session.user?.email)) {
    return { ok: false, error: "Family accounts only." };
  }
  const clean = (order ?? []).filter((s) => typeof s === "string").slice(0, 60);
  const cleanDrops = (drop ?? []).filter((s) => typeof s === "string").slice(0, 50);
  const cleanAdds = (add ?? [])
    .filter((a) => a && typeof a.ride_id === "string" && typeof a.ride_name === "string")
    .slice(0, 20);
  // An empty ORDER is legitimate when there are drops/adds — e.g. every
  // remaining ride is DOWN and the suggestion is "drop both" (hit for
  // real 2026-07-03). Only reject when there's literally nothing to do.
  if (!planId || (clean.length === 0 && cleanDrops.length === 0 && cleanAdds.length === 0)) {
    return { ok: false, error: "Nothing to apply." };
  }
  try {
    // Adds split into RESTORES (ride already in ride_sequence — it was
    // dropped, e.g. re-adding a ride that came back up; un-drop it so
    // the sequence isn't duplicated) vs genuinely NEW rides to append.
    // New rides land FIRST (so they exist in ride_sequence before the
    // order + poller reference them), then order, then drops.
    const existing = new Set(
      (await getReplanContext(planId))?.rides.map((r) => r.ride_id) ?? [],
    );
    const { restores, news } = splitReplanAdds(cleanAdds, existing);
    if (news.length) await addRidesToSequence(planId, news);
    await Promise.all(restores.map((id) => setRideDropped(planId, id, false)));
    if (clean.length) await setPlanOrder(planId, clean);
    await Promise.all(cleanDrops.map((id) => setRideDropped(planId, id, true)));
  } catch {
    return { ok: false, error: "Couldn't apply — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true };
}

/** America/New_York UTC offset (e.g. "-04:00") for a given ISO date. */
function etOffset(dateIso: string): string {
  try {
    const d = new Date(`${dateIso}T12:00:00Z`);
    const tz = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      timeZoneName: "longOffset",
    })
      .formatToParts(d)
      .find((p) => p.type === "timeZoneName")?.value;
    const off = (tz ?? "GMT-04:00").replace("GMT", "");
    return /^[+-]\d{2}:\d{2}$/.test(off) ? off : "-04:00";
  } catch {
    return "-04:00";
  }
}

/**
 * Record (or clear) a held Lightning Lane for a ride from /replan — the
 * self-serve version of set_held_ll. `time` is "HH:MM" (24h, from a time
 * input) or "" to clear; combined with the plan's date in ET.
 */
export async function applyHeldLl(
  planId: string,
  rideId: string,
  dateIso: string,
  time: string,
): Promise<ReplanResult> {
  const bad = await gate(planId, rideId);
  if (bad) return bad;
  let iso: string | null = null;
  if (time) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(time);
    if (!m) return { ok: false, error: "Enter a time like 3:00 PM." };
    const hh = String(Number(m[1])).padStart(2, "0");
    iso = `${dateIso}T${hh}:${m[2]}:00${etOffset(dateIso)}`;
  }
  try {
    await setHeldLl(planId, rideId, iso);
  } catch {
    return { ok: false, error: "Couldn't save — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true };
}

/**
 * Record an OPTIONAL actual wait (minutes) for a ride, or clear it
 * (empty). Never required — Mark done works without it; this just
 * captures calibration data (predicted vs actual) when the user offers it.
 */
export async function applyActualWait(
  planId: string,
  rideId: string,
  minutes: string,
): Promise<ReplanResult> {
  const bad = await gate(planId, rideId);
  if (bad) return bad;
  let val: number | null = null;
  if (minutes.trim() !== "") {
    const n = Number(minutes);
    if (!Number.isFinite(n) || n < 0 || n > 600) {
      return { ok: false, error: "Enter minutes (0–600)." };
    }
    val = Math.round(n);
  }
  try {
    await setRideActualWait(planId, rideId, val);
  } catch {
    return { ok: false, error: "Couldn't save — try again." };
  }
  revalidatePath("/replan");
  return { ok: true };
}

/**
 * Mark a ride done (done=true) or un-done (false) from /replan.
 * Done goes through completeRideAndAdvance (shared with the /done
 * one-tap link) so finishing your next_up ride advances next_up to the
 * following remaining ride and stamps next_up_since. Un-done stays a
 * plain set-DELETE — we don't try to guess the prior next_up back.
 */
export async function applyDone(
  planId: string,
  rideId: string,
  done: boolean,
): Promise<ReplanResult> {
  const bad = await gate(planId, rideId);
  if (bad) return bad;
  let llSuggestion: ReplanResult["ll_suggestion"];
  try {
    if (done) {
      const ctx = await getReplanContext(planId);
      if (!ctx) return { ok: false, error: "Plan not found." };
      await completeRideAndAdvance(planId, rideId, ctx);
      // The mark-done moment is when the family asks "what should we
      // book next?" — same hold-aware pick as /done and the poller
      // nudge. Best-effort: a live-read failure never fails the action.
      try {
        const live = await getParkRides(ctx.park_key);
        const gone = new Set([
          ...ctx.completed_ride_ids,
          ...ctx.dropped_ride_ids,
          rideId,
        ]);
        const pick = pickNextLl({
          rides: ctx.rides.filter((r) => !gone.has(r.ride_id)),
          holds: ctx.held_lls,
          live,
          now: new Date(),
        });
        if (pick) {
          llSuggestion = {
            ride_name: pick.ride_name,
            return_label: formatEtTime(pick.return_start),
            price: pick.price,
            standby_mins: pick.standby_mins,
          };
        }
      } catch {
        /* suggestion is a bonus */
      }
    } else {
      await setRideDone(planId, rideId, false);
    }
  } catch {
    return { ok: false, error: "Couldn't update — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true, ll_suggestion: llSuggestion };
}

/** Mark a ride "do next" (on=true) or clear the plan's next_up (on=false). */
export async function applyNextUp(
  planId: string,
  rideId: string,
  on: boolean,
): Promise<ReplanResult> {
  const bad = await gate(planId, rideId);
  if (bad) return bad;
  try {
    await setPlanNextUp(planId, on ? rideId : null);
  } catch {
    return { ok: false, error: "Couldn't update — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true };
}
