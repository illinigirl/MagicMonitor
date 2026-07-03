"use server";

/**
 * Server actions for /trips — the "get alerts for this trip" toggle.
 *
 * Same gates as the page: signed in + family allowlist. The member id
 * written is ALWAYS the session sub (never from the form), and the write
 * itself is the atomic set ADD/DELETE in dynamodb-writes.ts — see the
 * boundary note there (the web's only write into the shared partition).
 */

import { revalidatePath } from "next/cache";

import { auth } from "@/auth";
import { getUserProfile, setPlanAlertSubscription } from "@/lib/dynamodb-writes";
import { isTripsAllowed } from "@/lib/trips-access";

export interface ToggleResult {
  ok: boolean;
  error?: string;
}

export async function setTripAlerts(
  planIds: string[],
  subscribed: boolean,
): Promise<ToggleResult> {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) return { ok: false, error: "Not signed in." };
  if (!isTripsAllowed(session.user?.email)) {
    return { ok: false, error: "Family accounts only." };
  }

  // Sanity-bound the input (comes from the client component): plan ids
  // are ISO-timestamp SK suffixes. Reject junk rather than write it.
  const ids = (planIds ?? []).filter(
    (p) => typeof p === "string" && p.length > 0 && p.length < 100,
  );
  if (ids.length === 0 || ids.length > 30) {
    return { ok: false, error: "No plan days to update." };
  }

  // Receiving pushes needs a Pushover key on the member's profile — the
  // poller reads it from USER#<sub>/PROFILE. Guard opt-IN so the toggle
  // can't silently succeed at doing nothing; opt-OUT is always allowed.
  if (subscribed) {
    const profile = await getUserProfile(sub);
    if (!profile?.pushoverUserKey) {
      return {
        ok: false,
        error:
          "Add your Pushover key on the My alerts page first — that's where trip pushes get sent.",
      };
    }
  }

  try {
    await setPlanAlertSubscription(sub, ids, subscribed);
  } catch {
    return { ok: false, error: "Couldn't update — try again." };
  }
  revalidatePath("/trips");
  return { ok: true };
}
