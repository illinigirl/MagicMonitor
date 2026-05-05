"use client";

/**
 * Client wrapper for the favorites checkbox grid.
 *
 * Same pattern as /me's SettingsForm: useActionState for the result
 * banner, useFormStatus for the submit button's pending state. The
 * server passes the full ride list + initial favorites set; we never
 * re-fetch on the client. Re-render after save is driven by
 * revalidatePath on the server.
 */

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import type { ParkKey } from "@/lib/parks";
import { saveFavorites, type SaveFavoritesResult } from "./actions";

interface RideOption {
  ride_id: string;
  name: string;
  status: string;
}

interface Props {
  parkKey: ParkKey;
  rides: RideOption[];
  initialFavorites: string[];
}

export function FavoritesForm({ parkKey, rides, initialFavorites }: Props) {
  const action = saveFavorites.bind(null, parkKey);
  const [state, formAction] = useActionState<
    SaveFavoritesResult | null,
    FormData
  >(action, null);

  const favoriteSet = new Set(initialFavorites);

  return (
    <form action={formAction} className="space-y-6">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {rides.map((ride) => (
          <label
            key={ride.ride_id}
            className="flex items-center gap-3 rounded-md border border-line bg-bg-1 px-3 py-2 cursor-pointer hover:bg-bg-2"
          >
            <input
              type="checkbox"
              name="ride"
              value={ride.ride_id}
              defaultChecked={favoriteSet.has(ride.ride_id)}
              className="h-4 w-4 accent-gold"
            />
            <span className="text-fg-0 flex-1">{ride.name}</span>
            <RideStatusBadge status={ride.status} />
          </label>
        ))}
      </div>

      <div className="flex items-center gap-4">
        <SubmitButton />
        <StatusMessage state={state} />
      </div>
    </form>
  );
}

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="inline-flex items-center gap-2 rounded-md bg-gold px-4 py-2 text-sm font-medium text-gold-ink hover:opacity-90 disabled:opacity-60 transition-opacity"
    >
      {pending ? "Saving…" : "Save favorites"}
    </button>
  );
}

function StatusMessage({ state }: { state: SaveFavoritesResult | null }) {
  if (!state) return null;
  if (!state.ok) return <span className="text-sm text-bad">{state.error}</span>;
  if (state.addedCount === 0 && state.removedCount === 0) {
    return <span className="text-sm text-fg-2">No changes.</span>;
  }
  const parts: string[] = [];
  if (state.addedCount) parts.push(`+${state.addedCount}`);
  if (state.removedCount) parts.push(`−${state.removedCount}`);
  return (
    <span className="text-sm text-fg-2">
      Saved ({parts.join(", ")}) at{" "}
      {new Date(state.savedAt).toLocaleTimeString()}.
    </span>
  );
}

/** Tiny inline status hint so users see at-a-glance which rides are
 * actually running before deciding whether to favorite them. */
function RideStatusBadge({ status }: { status: string }) {
  const label =
    status === "OPERATING"
      ? "open"
      : status === "DOWN"
        ? "down"
        : status === "REFURBISHMENT"
          ? "refurb"
          : "closed";
  const color =
    status === "OPERATING"
      ? "text-ok"
      : status === "DOWN"
        ? "text-bad"
        : "text-fg-3";
  return <span className={`text-xs ${color}`}>{label}</span>;
}
