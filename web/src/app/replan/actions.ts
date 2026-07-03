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
import { setRideDropped } from "@/lib/dynamodb-writes";
import { isTripsAllowed } from "@/lib/trips-access";

export interface ReplanResult {
  ok: boolean;
  error?: string;
}

export async function applyDrop(
  planId: string,
  rideId: string,
  dropped: boolean,
): Promise<ReplanResult> {
  const session = await auth();
  if (!session?.user?.id) return { ok: false, error: "Not signed in." };
  if (!isTripsAllowed(session.user?.email)) {
    return { ok: false, error: "Family accounts only." };
  }
  if (!planId || !rideId || planId.length > 100 || rideId.length > 100) {
    return { ok: false, error: "Missing plan or ride." };
  }
  try {
    await setRideDropped(planId, rideId, dropped);
  } catch {
    return { ok: false, error: "Couldn't update — try again." };
  }
  revalidatePath("/replan");
  revalidatePath("/trips");
  return { ok: true };
}
