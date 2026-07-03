/**
 * Shared "mark done" semantics for a plan ride — used by both the
 * /replan Mark-done button (session-authed server action) and the /done
 * one-tap capability link (token-authed, reached from a Pushover alert).
 *
 * Keeping the compose logic here means the two entry points can never
 * drift: marking done always (a) adds to completed_ride_ids and (b) if
 * the ride WAS the plan's next_up, advances next_up to the first
 * remaining ride in the plan's order — stamping next_up_since, the
 * timestamp the "did you finish?" nudge and next-LL suggestion (PICKUP
 * #3) key off.
 */
import "server-only";
import { timingSafeEqual } from "crypto";

import { getReplanContext, type ReplanContext } from "./dynamodb";
import { setPlanNextUp, setRideDone } from "./dynamodb-writes";

export interface PlanRideRef {
  ride_id: string;
  ride_name: string;
}

/**
 * The ride that should become next_up after `justDoneId` completes:
 * the first ride in the plan's (already plan_order-sorted) sequence
 * that isn't completed, dropped, or the ride just finished. Null when
 * nothing remains. Pure — exported for tests.
 */
export function pickNextUp(
  rides: PlanRideRef[],
  completed: Iterable<string>,
  dropped: Iterable<string>,
  justDoneId: string,
): PlanRideRef | null {
  const gone = new Set([...completed, ...dropped, justDoneId]);
  return rides.find((r) => !gone.has(r.ride_id)) ?? null;
}

/**
 * Constant-time capability-token check. Length mismatch returns false
 * without leaking timing on the content; null/empty never matches.
 */
export function tokenMatches(
  expected: string | null | undefined,
  provided: string | null | undefined,
): boolean {
  if (!expected || !provided) return false;
  const a = Buffer.from(expected);
  const b = Buffer.from(provided);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export interface CompleteResult {
  ctx: ReplanContext;
  /** New next_up after the advance, null = plan finished or no advance
   *  needed (the done ride wasn't next_up). */
  advancedTo: PlanRideRef | null;
  /** True when this call moved next_up (the done ride was next_up). */
  advanced: boolean;
}

/**
 * Mark a ride done and advance next_up when the done ride held it.
 * The plan row is read BEFORE the write so the advance decision never
 * depends on read-after-write consistency. Throws if the plan doesn't
 * exist — callers translate to their own error shape.
 */
export async function completeRideAndAdvance(
  planId: string,
  rideId: string,
  preRead?: ReplanContext,
): Promise<CompleteResult> {
  const ctx = preRead ?? (await getReplanContext(planId));
  if (!ctx) throw new Error(`plan ${planId} not found`);

  await setRideDone(planId, rideId, true);

  if (ctx.next_up !== rideId) {
    return { ctx, advancedTo: null, advanced: false };
  }
  const next = pickNextUp(
    ctx.rides,
    ctx.completed_ride_ids,
    ctx.dropped_ride_ids,
    rideId,
  );
  await setPlanNextUp(planId, next?.ride_id ?? null);
  return { ctx, advancedTo: next, advanced: true };
}
