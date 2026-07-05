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
 *
 * Visuals: poster ticket motif — trips render as ticket-stub cards
 * with punch holes; the empty state is a dashed "ADMIT ZERO" ticket.
 */

import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  getMemberNames,
  getUpcomingTrips,
  SHARED_TRIP_OWNER_SUB,
  type Trip,
  type TripDay,
} from "@/lib/dynamodb";
import { formatEtTime as formatLlTime } from "@/lib/format-et";
import { buildDayTimeline } from "@/lib/plan-timeline";
import { isAndroidUa, mapsUrl } from "@/lib/nav-link";
import { findPark } from "@/lib/parks";
import { isTripsAllowed } from "@/lib/trips-access";
import { DiamondRule } from "@/components/retro";
import { FamilyOnly } from "@/components/auth/FamilyOnly";

import { getOrCreatePlanWidgetSecret } from "@/lib/dynamodb-writes";

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

/** Punch-hole circles overlapping a ticket's left/right edges. */
function PunchHoles({ dashed }: { dashed: boolean }) {
  const border = dashed ? "border-dashed" : "border-solid";
  return (
    <>
      <div
        className={`absolute -left-[13px] top-1/2 h-[22px] w-[22px] -translate-y-1/2 rounded-full border-2 border-line bg-bg-0 ${border}`}
        aria-hidden
      />
      <div
        className={`absolute -right-[13px] top-1/2 h-[22px] w-[22px] -translate-y-1/2 rounded-full border-2 border-line bg-bg-0 ${border}`}
        aria-hidden
      />
    </>
  );
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

  // Android devices get Google Maps nav links (native app); others Apple.
  const android = isAndroidUa((await headers()).get("user-agent"));

  // Today's-plan phone-widget token. Minted HERE (and only here) on
  // purpose: this page sits behind the family gate, so holding the
  // token proves the user passed it once (see dynamodb-writes.ts).
  const planWidgetSecret = await getOrCreatePlanWidgetSecret(viewerSub);

  return (
    <div className="mx-auto max-w-3xl px-6 md:px-10 pb-4">
      <header className="pt-10 text-center">
        <p className="kicker">Your trips</p>
        <h2 className="display mt-2.5 text-[40px] md:text-[56px] leading-[1.05] text-fg-0">
          UPCOMING TRIPS
        </h2>
        <DiamondRule />
        <p className="mx-auto mt-4 max-w-[640px] text-[15.5px] leading-[1.65] text-fg-2">
          Built in the planner and shared with the whole family. Each day is
          dormant until you activate it that morning — that&rsquo;s what
          turns on its live disruption alerts.
        </p>
      </header>

      {trips.length === 0 ? (
        <div className="relative mx-4 md:mx-16 mt-9 rounded-lg border-2 border-dashed border-line bg-bg-1 px-10 py-12 text-center">
          <PunchHoles dashed />
          <p className="font-head font-semibold text-[13px] uppercase tracking-[0.3em] text-fg-3">
            Admit zero
          </p>
          <p className="display mt-2.5 text-3xl text-fg-0">
            NO UPCOMING TRIPS YET
          </p>
          <p className="mx-auto mt-3 max-w-[520px] text-[14.5px] leading-relaxed text-fg-2">
            Ask Claude to build one in the planner — &ldquo;plan our June
            23&ndash;25 trip&rdquo; — and it&rsquo;ll show up here as a
            ticket.
          </p>
        </div>
      ) : (
        <div className="mt-9 space-y-8">
          {trips.map((trip) => (
            <TripTicket
              key={trip.trip_id}
              trip={trip}
              viewerSub={viewerSub}
              memberNames={memberNames}
              android={android}
            />
          ))}
        </div>
      )}

      <details className="group mx-4 md:mx-16 mt-8">
        <summary className="flex cursor-pointer list-none items-center gap-3 [&::-webkit-details-marker]:hidden">
          <span
            className="inline-block border-y-[6px] border-l-[9px] border-y-transparent border-l-accent transition-transform duration-100 group-open:rotate-90"
            aria-hidden
          />
          <span className="font-head font-semibold text-sm uppercase tracking-[0.14em] text-fg-0">
            Phone widget setup — today&rsquo;s plan
          </span>
        </summary>
        <div className="mt-3 space-y-2 text-sm text-fg-2">
          <p>
            Your private plan-feed URL (treat it like a password — it can
            read the family&rsquo;s plan):
          </p>
          <code className="block break-all rounded-[5px] border-2 border-line bg-bg-1 px-2 py-1 font-mono text-xs text-fg-1">
            {`https://magicmonitor.megillini.dev/api/widget/plan?t=${viewerSub}.${planWidgetSecret}`}
          </code>
          <p>
            Paste it into <code>tools/widget/scriptable-plan.js</code> (repo)
            in the Scriptable app to get a home-screen widget of today&rsquo;s
            schedule — rides, meals, shows, ✓ done, 🎟 holds.
          </p>
        </div>
      </details>
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

function TripTicket({
  trip,
  viewerSub,
  memberNames,
  android,
}: {
  trip: Trip;
  viewerSub: string;
  memberNames: Record<string, string>;
  android: boolean;
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
    <section className="relative rounded-lg border-2 border-line bg-bg-1 px-6 py-5 md:px-8">
      <PunchHoles dashed={false} />
      <div className="flex flex-wrap items-start justify-between gap-3 border-b-2 border-dashed border-line-soft pb-4">
        <div>
          <p className="font-head font-semibold text-[11px] uppercase tracking-[0.3em] text-fg-3">
            Admit · {formatDay(trip.start_date)} &ndash; {formatDay(trip.end_date)}{" "}
            · {trip.days.length} {trip.days.length === 1 ? "day" : "days"}
          </p>
          <h3 className="display mt-1.5 text-2xl text-fg-0">
            {trip.name ?? "Trip"}
          </h3>
          {openDays.length > 0 && (
            <p className="mt-1.5 text-xs text-fg-3">
              Getting alerts: {roster.join(" · ")}
            </p>
          )}
        </div>
        {openDays.length > 0 &&
          (viewerIsOwner ? (
            // Owner can't opt out of their own plan's alerts — show the
            // state instead of a toggle that would write a redundant row.
            <span className="shrink-0 rounded-full border-[1.5px] border-ok px-3 py-1 font-head font-semibold text-[11px] uppercase tracking-[0.14em] text-ok">
              You&rsquo;re alerted (your plan)
            </span>
          ) : (
            <TripAlertToggle
              planIds={openDays.map((d) => d.plan_id)}
              subscribed={subscribed}
            />
          ))}
      </div>
      <div className="divide-y-2 divide-dashed divide-line-soft">
        {trip.days.map((day) => (
          <DayRow key={day.plan_id} day={day} android={android} />
        ))}
      </div>
    </section>
  );
}

function DayRow({ day, android }: { day: TripDay; android: boolean }) {
  const park = findPark(day.park_key);
  return (
    <div className="flex gap-4 py-4">
      {/* Park code chip in the park's poster accent */}
      <div
        className="display w-10 shrink-0 pt-0.5 text-[15px]"
        style={{ color: `var(${park?.accentVar ?? "--accent"})` }}
        aria-hidden
      >
        {park?.shortName ?? "?"}
      </div>
      <div className="flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <p
              className="font-head font-semibold text-[15px] uppercase tracking-[0.06em] text-fg-0"
            >
              {park?.name ?? day.park_key}
            </p>
            <p className="label-meta mt-0.5">{formatDay(day.date)}</p>
          </div>
          <DayStatus active={day.active} outcomeRecorded={day.outcome_recorded} />
        </div>
        {(() => {
          const timeline = buildDayTimeline(
            day.rides,
            day.reservations,
            day.shows,
          );
          if (timeline.length === 0) {
            return <p className="mt-3 text-sm text-fg-3">No rides lined up yet.</p>;
          }
          return (
            <ul className="mt-3 flex flex-wrap gap-x-2 gap-y-1 text-sm text-fg-2">
              {timeline.map((entry, i) => {
                const sep = i < timeline.length - 1 && (
                  <span className="text-fg-3" aria-hidden>
                    {" "}·
                  </span>
                );
                const parkName = findPark(day.park_key)?.name;
                if (entry.kind !== "ride") {
                  const icon =
                    entry.kind === "show" ? "🎭" : entry.booked ? "🍽" : "🥪";
                  return (
                    <li key={`x-${i}`}>
                      <span aria-hidden>{icon}</span>{" "}
                      <span className="text-fg-3 text-xs tabular-nums">
                        {formatLlTime(entry.time)}
                      </span>{" "}
                      <a
                        href={mapsUrl({ name: entry.name, parkName, android })}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:underline"
                        title="Navigate there"
                      >
                        {entry.name}
                      </a>
                      {sep}
                    </li>
                  );
                }
                const r = entry.ride;
                return (
                  <li
                    key={`${r.ride_id ?? r.ride_name}-${i}`}
                    className={r.done ? "text-fg-3" : undefined}
                  >
                    {r.done && <span aria-label="done">✓ </span>}
                    {r.target_time && !r.done && (
                      <span className="text-fg-3 text-xs tabular-nums">
                        {formatLlTime(r.target_time)}{" "}
                      </span>
                    )}
                    <a
                      href={mapsUrl({
                        ride_id: r.ride_id,
                        name: r.ride_name,
                        parkName,
                        android,
                      })}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline"
                      title="Navigate there"
                    >
                      {r.ride_name}
                    </a>
                    {r.held_ll && !r.done && (
                      <span className="text-accent text-xs" title="Lightning Lane held">
                        {" "}🎟 {formatLlTime(r.held_ll)}
                      </span>
                    )}
                    {sep}
                  </li>
                );
              })}
            </ul>
          );
        })()}
        {/* Adjust entry point — active, in-play days only. Reaches the
            same /replan surface an alert links to, so the plan is
            adjustable from the dashboard, not just from a live push. */}
        {day.active && !day.outcome_recorded && day.rides.length > 0 && (
          <a
            href={`/replan?plan=${encodeURIComponent(day.plan_id)}`}
            className="poster-link mt-3 inline-block rounded-[5px] border-2 border-accent px-3 py-1.5 text-accent transition-colors duration-100 hover:bg-accent hover:text-bg-0"
          >
            Today&rsquo;s schedule — waits, mark done, LLs →
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
  const base =
    "shrink-0 rounded-full border-[1.5px] px-2.5 py-[3px] font-head font-semibold text-[10px] uppercase tracking-[0.14em]";
  if (outcomeRecorded) {
    return <span className={`${base} border-fg-3 text-fg-3`}>Recorded</span>;
  }
  if (active) {
    return <span className={`${base} border-ok text-ok`}>Monitoring</span>;
  }
  return <span className={`${base} border-fg-3 text-fg-3`}>Dormant</span>;
}
