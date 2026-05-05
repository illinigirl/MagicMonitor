/**
 * Server action for /me/rides/[park] — save the user's favorite-ride
 * selection for one park.
 *
 * Same auth contract as the /me settings action: read auth().user.id
 * for the partition-key prefix, never trust a client-supplied user
 * id. Same diff-then-write pattern as park toggles, scaled to N rides.
 *
 * The action re-fetches the park's ride list so it can: (a) reject
 * rideIds that aren't actually in this park (defends against
 * hand-crafted POSTs), and (b) attach the human-readable ride name
 * to the FAV_RIDE row (used by future cross-park views without
 * needing a join against RIDE# state).
 */
"use server";

import { revalidatePath } from "next/cache";

import { auth } from "@/auth";
import { getParkRides } from "@/lib/dynamodb";
import {
  getUserFavoriteRides,
  setFavoriteRide,
} from "@/lib/dynamodb-writes";
import { findPark, type ParkKey } from "@/lib/parks";

export type SaveFavoritesResult =
  | { ok: true; savedAt: string; addedCount: number; removedCount: number }
  | { ok: false; error: string };

export async function saveFavorites(
  parkKey: ParkKey,
  _prevState: SaveFavoritesResult | null,
  formData: FormData,
): Promise<SaveFavoritesResult> {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) return { ok: false, error: "Not signed in." };

  if (!findPark(parkKey)) {
    return { ok: false, error: `Unknown park: ${parkKey}` };
  }

  // Source of truth for "what rides exist in this park" is the
  // RIDE# STATE rows the poller maintains. We reject anything not in
  // that set so a hand-crafted POST can't seed bogus FAV_RIDE rows.
  const rides = await getParkRides(parkKey);
  const rideById = new Map(rides.map((r) => [r.ride_id, r]));

  const requested = new Set<string>(
    formData
      .getAll("ride")
      .map((v) => v.toString())
      .filter((id) => rideById.has(id)),
  );

  const current = await getUserFavoriteRides(sub, parkKey);
  const toAdd: string[] = [];
  const toRemove: string[] = [];
  for (const id of requested) {
    if (!current.has(id)) toAdd.push(id);
  }
  for (const id of current) {
    if (!requested.has(id)) toRemove.push(id);
  }

  await Promise.all([
    ...toAdd.map((id) => {
      const ride = rideById.get(id)!;
      return setFavoriteRide(sub, id, parkKey, ride.name, true);
    }),
    ...toRemove.map((id) => setFavoriteRide(sub, id, parkKey, "", false)),
  ]);

  revalidatePath(`/me/rides/${parkKey}`);

  return {
    ok: true,
    savedAt: new Date().toISOString(),
    addedCount: toAdd.length,
    removedCount: toRemove.length,
  };
}
