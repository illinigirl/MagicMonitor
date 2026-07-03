/**
 * /replan — the one-tap re-plan approve page, reached from a disruption
 * alert's Pushover deep-link (?plan=<id>&ride=<id>). No Claude app
 * needed: anyone in the family with the Pushover alert + a browser can
 * act. Human-in-the-loop — it proposes, you Approve/Dismiss.
 *
 * v1 handles the ride-down case with a heuristic ("drop it / keep it")
 * and shows the rest of the day's remaining rides for context. A smarter
 * Claude-backed re-sequence is the planned next step (cost-gated).
 */

import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { getReplanContext } from "@/lib/dynamodb";
import { isTripsAllowed } from "@/lib/trips-access";

import ReplanControls from "./ReplanControls";

export const dynamic = "force-dynamic";

export default async function ReplanPage({
  searchParams,
}: {
  searchParams: Promise<{ plan?: string; ride?: string }>;
}) {
  const session = await auth();
  if (!session?.user?.id) {
    redirect("/api/auth/signin?callbackUrl=/replan");
  }
  if (!isTripsAllowed(session.user?.email)) {
    return (
      <div className="mx-auto max-w-md px-6 py-16 text-center">
        <p className="text-fg-1">This trip is family-only.</p>
      </div>
    );
  }

  const sp = await searchParams;
  const planId = sp.plan ?? "";
  const rideId = sp.ride ?? "";
  const ctx = planId ? await getReplanContext(planId) : null;

  if (!ctx) {
    return (
      <div className="mx-auto max-w-md px-6 py-16 text-center">
        <p className="text-fg-1 font-medium">Plan not found.</p>
        <p className="text-fg-3 text-sm mt-1">
          It may have been recorded or removed. See{" "}
          <a href="/trips" className="underline">your trips</a>.
        </p>
      </div>
    );
  }

  const affected = ctx.rides.find((r) => r.ride_id === rideId);
  const droppedSet = new Set(ctx.dropped_ride_ids);
  const remaining = ctx.rides.filter(
    (r) => r.ride_id !== rideId && !droppedSet.has(r.ride_id),
  );

  return (
    <div className="mx-auto max-w-md px-6 py-12">
      <p className="label-meta">Re-plan · {ctx.park_name}</p>
      <h2 className="display text-2xl font-medium mt-2">
        {affected ? `${affected.ride_name} was disrupted` : "Adjust today’s plan"}
      </h2>

      {affected ? (
        <div className="mt-5 rounded-lg border border-line bg-bg-1 p-4 shadow-[var(--shadow-card)]">
          <p className="text-fg-1 text-sm mb-3">
            {affected.ride_name} is in today’s plan. Drop it so you stop
            getting alerts and it’s out of your sequence, or keep it in case
            it comes back up.
          </p>
          <ReplanControls
            planId={ctx.plan_id}
            rideId={affected.ride_id}
            rideName={affected.ride_name}
            initiallyDropped={droppedSet.has(affected.ride_id)}
          />
        </div>
      ) : (
        <p className="mt-4 text-fg-2 text-sm">
          That ride isn’t in this plan (already dropped or completed).
        </p>
      )}

      <div className="mt-8">
        <h3 className="label-meta mb-2">
          {remaining.length > 0 ? "Still on today’s plan" : "Nothing else queued"}
        </h3>
        <div className="rounded-lg border border-line bg-bg-1 divide-y divide-line-soft shadow-[var(--shadow-card)]">
          {remaining.map((r, i) => (
            <div key={r.ride_id} className="flex items-center gap-3 px-4 py-2.5">
              <span className="text-fg-3 text-xs w-5">{i + 1}.</span>
              <span className="text-fg-0 text-sm">{r.ride_name}</span>
            </div>
          ))}
          {remaining.length === 0 && (
            <p className="px-4 py-3 text-fg-3 text-sm">
              No other rides left in the sequence.
            </p>
          )}
        </div>
        <p className="mt-4 text-fg-3 text-xs">
          <a href="/trips" className="underline">Open the full trip</a> to see
          every day.
        </p>
      </div>
    </div>
  );
}
