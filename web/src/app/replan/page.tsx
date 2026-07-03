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
import { getParkRides, getReplanContext } from "@/lib/dynamodb";
import { isTripsAllowed } from "@/lib/trips-access";
import { FamilyOnly } from "@/components/auth/FamilyOnly";

import ReplanControls from "./ReplanControls";
import AskClaude from "./AskClaude";
import HeldLlInput from "./HeldLlInput";

export const dynamic = "force-dynamic";

export default async function ReplanPage({
  searchParams,
}: {
  searchParams: Promise<{ plan?: string; ride?: string; type?: string }>;
}) {
  const session = await auth();
  if (!session?.user?.id) {
    redirect("/api/auth/signin?callbackUrl=/replan");
  }
  if (!isTripsAllowed(session.user?.email)) {
    return <FamilyOnly email={session.user?.email} />;
  }

  const sp = await searchParams;
  const planId = sp.plan ?? "";
  const rideId = sp.ride ?? "";
  // Alert kind sets which action leads. "down" → Drop; "next" (short
  // wait / earlier LL / back-up) → Do next; "storm"/absent → neutral.
  const kind = sp.type ?? "";
  const ctx = planId ? await getReplanContext(planId) : null;
  // Live waits for the plan's park, to show current wait per ride.
  const liveWaits = ctx ? await getParkRides(ctx.park_key) : [];
  const waitById = new Map(liveWaits.map((r) => [r.ride_id, r]));

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
  const doneSet = new Set(ctx.completed_ride_ids);
  // "down" alerts lead with Drop; everything else (short wait, earlier
  // LL, back-up) leads with Do next. The affected ride follows the alert
  // kind; other rides default to Drop-lead.
  const affectedEmphasis: "drop" | "next" = kind === "down" ? "drop" : "next";

  const heading = affected
    ? kind === "down"
      ? `${affected.ride_name} was disrupted`
      : `${affected.ride_name} — do it next?`
    : kind === "storm"
      ? "Storm coming — adjust your day"
      : "Today’s plan";
  const lede = affected
    ? kind === "down"
      ? "Drop it so you stop getting alerts and it’s out of your sequence, or leave it — it may come back up."
      : "Short wait / earlier LL — mark it “Do next,” or adjust any other ride."
    : kind === "storm"
      ? "Disney pauses outdoor rides in a storm. Drop outdoor rides or mark an indoor one “Do next.”"
      : "Mark a ride “Do next,” or drop one you’re skipping. Undo anytime.";

  return (
    <div className="mx-auto max-w-md px-6 py-12">
      <p className="label-meta">Adjust plan · {ctx.park_name}</p>
      <h2 className="display text-2xl font-medium mt-2">{heading}</h2>
      <p className="text-fg-2 text-sm mt-2">{lede}</p>

      <div className="mt-6">
        <AskClaude
          planId={ctx.plan_id}
          trigger={affected ? `${affected.ride_name} (${kind || "alert"})` : null}
          rideNames={Object.fromEntries(ctx.rides.map((r) => [r.ride_id, r.ride_name]))}
        />
      </div>

      <div className="rounded-lg border border-line bg-bg-1 divide-y divide-line-soft shadow-[var(--shadow-card)]">
        {ctx.rides.map((r, i) => {
          const isAffected = r.ride_id === rideId;
          const isNext = ctx.next_up === r.ride_id;
          return (
            <div
              key={r.ride_id}
              className={
                "px-4 py-3 " + (isAffected ? "bg-warn/5" : isNext ? "bg-ok/5" : "")
              }
            >
              <div className="flex items-center gap-3">
                <span className="text-fg-3 text-xs w-5">{i + 1}.</span>
                <span className="text-fg-0 text-sm flex-1">{r.ride_name}</span>
                <CurrentWait live={waitById.get(r.ride_id)} />
                {isAffected && (
                  <span className="rounded-full bg-warn/15 px-2 py-0.5 text-xs text-warn">
                    alert
                  </span>
                )}
              </div>
              <div className="mt-2 pl-8">
                <HeldLlInput
                  planId={ctx.plan_id}
                  rideId={r.ride_id}
                  dateIso={ctx.date}
                  heldIso={ctx.held_lls[r.ride_id] ?? null}
                />
              </div>
              <div className="mt-2 pl-8">
                <ReplanControls
                  planId={ctx.plan_id}
                  rideId={r.ride_id}
                  rideName={r.ride_name}
                  initiallyDropped={droppedSet.has(r.ride_id)}
                  initiallyNext={isNext}
                  initiallyDone={doneSet.has(r.ride_id)}
                  emphasize={isAffected ? affectedEmphasis : "drop"}
                />
              </div>
            </div>
          );
        })}
        {ctx.rides.length === 0 && (
          <p className="px-4 py-3 text-fg-3 text-sm">
            No rides left in this plan’s sequence.
          </p>
        )}
      </div>

      <p className="mt-4 text-fg-3 text-xs">
        Waits are live (poller refreshes every ~2 min) ·{" "}
        <a href="/trips" className="underline">All trips &amp; days →</a>
      </p>
    </div>
  );
}

/** Compact current-wait chip for a ride, from the live STATE row. */
function CurrentWait({
  live,
}: {
  live?: { status: string; wait_mins: number | null };
}) {
  if (!live) return null;
  if (live.status === "DOWN")
    return (
      <span className="shrink-0 rounded-full bg-bad/15 px-2 py-0.5 text-xs font-medium text-bad">
        Down
      </span>
    );
  if (live.status === "OPERATING" && live.wait_mins !== null)
    return (
      <span className="shrink-0 text-fg-0 text-sm font-semibold tabular-nums">
        {live.wait_mins}
        <span className="text-fg-3 text-xs font-normal ml-0.5">min</span>
      </span>
    );
  return (
    <span className="shrink-0 text-fg-3 text-xs">
      {live.status === "OPERATING" ? "open" : live.status.toLowerCase()}
    </span>
  );
}
