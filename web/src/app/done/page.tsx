/**
 * /done — one-tap "✓ mark this ride done" from a Pushover alert.
 *
 * Capability-token authed (?plan=<id>&ride=<id>&t=<done_token>): NO
 * Cognito session, so it works in any browser — including Pushover's
 * in-app browser with its separate cookie jar. The token lives on the
 * plan row (see getOrCreatePlanDoneToken) and is only ever distributed
 * inside the family's own alerts.
 *
 * Deliberate: the GET performs the write. The URL never appears
 * anywhere a link-previewer would fetch it (Pushover supplementary
 * URLs aren't prefetched), the write is an idempotent set-add, and
 * Undo is one tap away — so the one-tap ergonomics win over
 * POST-redirect ceremony. If a prefetcher ever DOES burn one, the
 * confirmation page makes the state visible and reversible.
 *
 * Marking done advances next_up when the done ride held it (shared
 * semantics with /replan's Mark done — see lib/plan-complete.ts).
 */
import { getReplanContext } from "@/lib/dynamodb";
import { getPlanDoneToken, setRideDone } from "@/lib/dynamodb-writes";
import {
  completeRideAndAdvance,
  pickNextUp,
  tokenMatches,
  type PlanRideRef,
} from "@/lib/plan-complete";

export const dynamic = "force-dynamic";

export default async function DonePage({
  searchParams,
}: {
  searchParams: Promise<{
    plan?: string;
    ride?: string;
    t?: string;
    undo?: string;
  }>;
}) {
  const sp = await searchParams;
  const planId = (sp.plan ?? "").slice(0, 100);
  const rideId = (sp.ride ?? "").slice(0, 100);
  const token = (sp.t ?? "").slice(0, 100);
  const undo = sp.undo === "1";

  if (!planId || !rideId || !token) {
    return <Notice title="Link not valid" body="Missing plan, ride, or token." />;
  }

  const expected = await getPlanDoneToken(planId);
  if (!tokenMatches(expected, token)) {
    return (
      <Notice
        title="Link not valid"
        body="This done-link doesn't match the plan. Open the schedule instead."
        planLink={planId}
      />
    );
  }

  // Token verified from here down — the holder may write to THIS plan.
  const ctx = await getReplanContext(planId);
  if (!ctx) {
    return <Notice title="Plan not found" body="It may have been removed." />;
  }
  if (ctx.outcome_recorded) {
    return (
      <Notice
        title="This day is wrapped up"
        body="The plan's outcome was already recorded, so there's nothing to mark."
        planLink={planId}
      />
    );
  }
  const ride = ctx.rides.find((r) => r.ride_id === rideId);
  if (!ride) {
    return (
      <Notice
        title="Ride not in this plan"
        body="It may have been removed from the sequence."
        planLink={planId}
      />
    );
  }

  const doneUrl = (r: string, undoFlag: boolean) =>
    `/done?plan=${encodeURIComponent(planId)}&ride=${encodeURIComponent(r)}` +
    `&t=${encodeURIComponent(token)}${undoFlag ? "&undo=1" : ""}`;

  if (undo) {
    await setRideDone(planId, rideId, false);
    return (
      <Shell park={ctx.park_name}>
        <h2 className="display text-2xl font-medium mt-2">
          {ride.ride_name} is back on the list
        </h2>
        <p className="text-fg-2 text-sm mt-2">
          Marked not done. (If it was your “do next,” re-pick that on the
          schedule.)
        </p>
        <Links planId={planId} extra={{ href: doneUrl(rideId, false), label: "Mark done again ✓" }} />
      </Shell>
    );
  }

  const { advancedTo, advanced } = await completeRideAndAdvance(
    planId,
    rideId,
    ctx,
  );
  // When next_up didn't move (the done ride wasn't it), still show
  // what's likely next so the page is useful at a glance.
  const upNext: PlanRideRef | null = advanced
    ? advancedTo
    : ctx.next_up
      ? (ctx.rides.find((r) => r.ride_id === ctx.next_up) ?? null)
      : pickNextUp(ctx.rides, ctx.completed_ride_ids, ctx.dropped_ride_ids, rideId);

  return (
    <Shell park={ctx.park_name}>
      <h2 className="display text-2xl font-medium mt-2">
        ✓ {ride.ride_name} — done
      </h2>
      <p className="text-fg-2 text-sm mt-2">
        {upNext
          ? advanced
            ? `Next up: ${upNext.ride_name}.`
            : `Still up next: ${upNext.ride_name}.`
          : "That was the last one — nice day! 🎉"}
      </p>
      <Links
        planId={planId}
        extra={{ href: doneUrl(rideId, true), label: "Undo" }}
      />
    </Shell>
  );
}

function Shell({ park, children }: { park: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-md px-6 py-16">
      <p className="label-meta">Today’s plan · {park}</p>
      {children}
    </div>
  );
}

function Links({
  planId,
  extra,
}: {
  planId: string;
  extra?: { href: string; label: string };
}) {
  return (
    <p className="mt-6 text-sm">
      <a href={`/replan?plan=${encodeURIComponent(planId)}`} className="underline">
        Open the full schedule →
      </a>
      {extra && (
        <>
          {" · "}
          <a href={extra.href} className="underline text-fg-2">
            {extra.label}
          </a>
        </>
      )}
    </p>
  );
}

function Notice({
  title,
  body,
  planLink,
}: {
  title: string;
  body: string;
  planLink?: string;
}) {
  return (
    <div className="mx-auto max-w-md px-6 py-16 text-center">
      <p className="text-fg-1 font-medium">{title}</p>
      <p className="text-fg-3 text-sm mt-1">{body}</p>
      {planLink && (
        <p className="text-sm mt-4">
          <a
            href={`/replan?plan=${encodeURIComponent(planLink)}`}
            className="underline"
          >
            Open the schedule →
          </a>
        </p>
      )}
    </div>
  );
}
