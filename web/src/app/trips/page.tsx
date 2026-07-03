/**
 * /trips — read-only view of the shared family trip(s).
 *
 * Unlike /me (per-user data under USER#<sub>), this reads the SHARED
 * trip space (USER#megan) the MCP planner writes to — so it's gated to
 * the family by an email allowlist (TRIPS_ALLOWED_EMAILS), not just
 * "any logged-in user." A random Google sign-in can use /me with their
 * own data, but must not see the family's trip.
 *
 * Days + date ranges are derived from the PLAN# rows in the read helper
 * (the (Y) model), so they never drift from the trip's actual days.
 */

import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  getMemberNames,
  getUpcomingTrips,
  SHARED_TRIP_OWNER_SUB,
  type Trip,
  type TripDay,
} from "@/lib/dynamodb";
import { findPark } from "@/lib/parks";
import { isTripsAllowed } from "@/lib/trips-access";
import { FamilyOnly } from "@/components/auth/FamilyOnly";

import TripAlertToggle from "./TripAlertToggle";

// Shared, family-scoped, low-traffic — always render fresh.
export const dynamic = "force-dynamic";

/** "2099-09-01" → "Mon, Sep 1" without a timezone shift (date-only). */
function formatDay(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export default async function TripsPage() {
  const session = await auth();
  if (!session?.user) {
    // Redirect to the sign-in PAGE (not the provider endpoint): a GET
    // to /api/auth/signin/cognito has no CSRF token, so Auth.js v5 fails
    // it as a "Configuration" error. The page handles CSRF + the bounce
    // to Cognito → Google itself.
    redirect("/api/auth/signin?callbackUrl=/trips");
  }

  // Family-only gate: this surface shows shared data.
  if (!isTripsAllowed(session.user.email)) {
    return <FamilyOnly email={session.user.email} />;
  }

  const trips = await getUpcomingTrips();
  const viewerSub = session?.user?.id ?? "";

  // Resolve every subscriber sub (across all trips) + the owner to names,
  // once, so each trip can show its "who's getting alerts" roster.
  const allSubs = new Set<string>([SHARED_TRIP_OWNER_SUB]);
  for (const t of trips) {
    for (const d of t.days) for (const s of d.alert_subscribers) allSubs.add(s);
  }
  const memberNames = await getMemberNames([...allSubs]);

  return (
    <div className="mx-auto max-w-2xl px-6 py-12">
      <header className="mb-8">
        <p className="label-meta">Your trips</p>
        <h2 className="display text-3xl font-medium mt-2">Upcoming trips</h2>
        <p className="text-fg-2 mt-2 leading-relaxed">
          The shared family plan, built in the planner. Each day is dormant
          until you activate it that morning — that&rsquo;s what turns on
          its live disruption alerts.
        </p>
      </header>

      {trips.length === 0 ? (
        <div className="rounded-lg border border-line bg-bg-1 px-5 py-8 text-center shadow-[var(--shadow-card)]">
          <p className="text-fg-1 font-medium">No upcoming trips yet.</p>
          <p className="text-fg-3 text-sm mt-1">
            Ask Claude to build one in the planner — &ldquo;plan our June
            23&ndash;25 trip&rdquo; — and it&rsquo;ll show up here.
          </p>
        </div>
      ) : (
        <div className="space-y-10">
          {trips.map((trip) => (
            <TripSection
              key={trip.trip_id}
              trip={trip}
              viewerSub={viewerSub}
              memberNames={memberNames}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Display label for a subscriber sub — "You" for the viewer, else the
 *  resolved profile name. */
function labelFor(
  sub: string,
  memberNames: Record<string, string>,
  viewerSub: string,
): string {
  if (sub === viewerSub) return "You";
  return memberNames[sub] ?? `${sub.slice(0, 6)}…`;
}

function TripSection({
  trip,
  viewerSub,
  memberNames,
}: {
  trip: Trip;
  viewerSub: string;
  memberNames: Record<string, string>;
}) {
  // The toggle applies to days still in play (recorded days are history —
  // their alerts can't fire again).
  const openDays = trip.days.filter((d) => !d.outcome_recorded);

  // The plan owner is alerted implicitly (server-side) and is never in
  // alert_subscribers — so their toggle must not depend on the set.
  const viewerIsOwner = viewerSub === SHARED_TRIP_OWNER_SUB;
  const subscribed =
    viewerIsOwner ||
    openDays.some((d) => d.alert_subscribers.includes(viewerSub));

  // Roster: everyone getting alerts = owner (always) + the union of
  // stored subscribers across open days, resolved to names. Dedupe the
  // owner out of the subscriber list so they aren't shown twice.
  const subscriberSubs = new Set<string>();
  for (const d of openDays) for (const s of d.alert_subscribers) subscriberSubs.add(s);
  subscriberSubs.delete(SHARED_TRIP_OWNER_SUB);
  const roster = [
    labelFor(SHARED_TRIP_OWNER_SUB, memberNames, viewerSub) + " (owner)",
    ...[...subscriberSubs]
      .map((s) => labelFor(s, memberNames, viewerSub))
      .sort(),
  ];

  return (
    <section>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 className="display text-xl font-medium text-fg-1">
            {trip.name ?? "Trip"}
          </h3>
          <p className="label-meta mt-1">
            {formatDay(trip.start_date)} &ndash; {formatDay(trip.end_date)} ·{" "}
            {trip.days.length} {trip.days.length === 1 ? "day" : "days"}
          </p>
          {openDays.length > 0 && (
            <p className="text-fg-3 text-xs mt-1.5">
              Getting alerts: {roster.join(" · ")}
            </p>
          )}
        </div>
        {openDays.length > 0 &&
          (viewerIsOwner ? (
            // Owner can't opt out of their own plan's alerts — show the
            // state instead of a toggle that would write a redundant row.
            <span className="shrink-0 rounded-full border border-ok/40 bg-ok/15 px-3 py-1 text-xs font-medium text-ok">
              You&rsquo;re alerted (your plan)
            </span>
          ) : (
            <TripAlertToggle
              planIds={openDays.map((d) => d.plan_id)}
              subscribed={subscribed}
            />
          ))}
      </div>
      <div className="space-y-3">
        {trip.days.map((day) => (
          <DayCard key={day.plan_id} day={day} />
        ))}
      </div>
    </section>
  );
}

function DayCard({ day }: { day: TripDay }) {
  const park = findPark(day.park_key);
  return (
    <div className="flex gap-3 rounded-lg border border-line bg-bg-1 shadow-[var(--shadow-card)] overflow-hidden">
      {/* per-park accent strip */}
      <div
        className="w-1 shrink-0"
        style={{ background: `var(${park?.accentVar ?? "--gold"})` }}
        aria-hidden
      />
      <div className="flex-1 px-4 py-3">
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <p className="font-medium text-fg-0">{park?.name ?? day.park_key}</p>
            <p className="label-meta mt-0.5">{formatDay(day.date)}</p>
          </div>
          <DayStatus active={day.active} outcomeRecorded={day.outcome_recorded} />
        </div>
        {day.rides.length > 0 ? (
          <ul className="mt-3 flex flex-wrap gap-x-2 gap-y-1 text-sm text-fg-2">
            {day.rides.map((r, i) => (
              <li key={`${r.ride_id ?? r.ride_name}-${i}`}>
                {r.ride_name}
                {i < day.rides.length - 1 && (
                  <span className="text-fg-3" aria-hidden>
                    {" "}·
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-fg-3">No rides lined up yet.</p>
        )}
        {/* Adjust entry point — active, in-play days only. Reaches the
            same /replan surface an alert links to, so the plan is
            adjustable from the dashboard, not just from a live push. */}
        {day.active && !day.outcome_recorded && day.rides.length > 0 && (
          <a
            href={`/replan?plan=${encodeURIComponent(day.plan_id)}`}
            className="mt-3 inline-block text-xs text-gold hover:underline"
          >
            Adjust / drop rides →
          </a>
        )}
      </div>
    </div>
  );
}

function DayStatus({
  active,
  outcomeRecorded,
}: {
  active: boolean;
  outcomeRecorded: boolean;
}) {
  if (outcomeRecorded) {
    return (
      <span className="shrink-0 rounded-full bg-bg-3 px-2.5 py-0.5 text-xs font-medium text-fg-2">
        Recorded
      </span>
    );
  }
  if (active) {
    return (
      <span className="shrink-0 rounded-full bg-ok/15 px-2.5 py-0.5 text-xs font-medium text-ok">
        Monitoring
      </span>
    );
  }
  return (
    <span className="shrink-0 rounded-full bg-bg-3 px-2.5 py-0.5 text-xs font-medium text-fg-3">
      Dormant
    </span>
  );
}
