/**
 * /waits — the per-user glance page: live waits for YOUR rides.
 *
 * The phone-widget companion (the successor to the Pi version's widget
 * feed): favorites across all parks on one page, grouped by park, with
 * today's ACTIVE plan pinned on top and current WDW weather in the
 * header. Filter chips narrow to one park (?park=) for anyone who finds
 * all-at-once busy. Auto-refreshes every 60s via meta refresh — the
 * poller writes every 2 min, so that's always ≤1 poll stale.
 *
 * The widget feed URL (a per-user capability token — see
 * dynamodb-writes.ts) is exposed in a <details> at the bottom, paired
 * with tools/widget/scriptable-waits.js for the actual iOS widget.
 */

import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { getMyWaits, type MyWaitRide } from "@/lib/my-waits";
import { getOrCreateWidgetSecret } from "@/lib/dynamodb-writes";
import { findPark, type ParkKey } from "@/lib/parks";
import { getCurrentConditions } from "@/lib/weather";

export const dynamic = "force-dynamic";

export default async function WaitsPage({
  searchParams,
}: {
  searchParams: Promise<{ park?: string }>;
}) {
  const session = await auth();
  if (!session?.user?.id) {
    redirect("/api/auth/signin?callbackUrl=/waits");
  }
  const sub = session.user.id;

  const sp = await searchParams;
  const parkFilter = (findPark(sp.park ?? "")?.key ?? null) as ParkKey | null;

  const [waits, weather, widgetSecret] = await Promise.all([
    getMyWaits(sub),
    getCurrentConditions(),
    getOrCreateWidgetSecret(sub),
  ]);

  const groups = parkFilter
    ? waits.parks.filter((g) => g.park_key === parkFilter)
    : waits.parks;
  const showPlan =
    waits.plan && (!parkFilter || waits.plan.park_key === parkFilter);

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      {/* 60s auto-refresh: a glance page must never show stale-forever data. */}
      <meta httpEquiv="refresh" content="60" />

      <header className="mb-6 flex items-baseline justify-between gap-4">
        <div>
          <p className="label-meta">My waits</p>
          <h2 className="display text-3xl font-medium mt-2">Your rides now</h2>
        </div>
        {weather && (
          <p className="shrink-0 text-fg-1 text-lg" title={weather.condition}>
            {weather.icon} {weather.temp_f}°
            <span className="text-fg-3 text-sm ml-1.5">{weather.condition}</span>
          </p>
        )}
      </header>

      {waits.parks.length > 1 && (
        <nav className="mb-6 flex flex-wrap gap-2">
          <Chip href="/waits" active={!parkFilter} label="All parks" />
          {waits.parks.map((g) => (
            <Chip
              key={g.park_key}
              href={`/waits?park=${g.park_key}`}
              active={parkFilter === g.park_key}
              label={findPark(g.park_key)?.shortName ?? g.park_key}
            />
          ))}
        </nav>
      )}

      {showPlan && waits.plan && (
        <section className="mb-8">
          <h3 className="label-meta mb-2">
            Today&rsquo;s plan · {waits.plan.park_name}
          </h3>
          <div className="rounded-lg border border-gold/40 bg-bg-1 shadow-[var(--shadow-card)] divide-y divide-line-soft">
            {waits.plan.rides.map((r, i) => (
              <WaitRow key={`${r.ride_id}-${i}`} ride={r} ordinal={i + 1} />
            ))}
          </div>
        </section>
      )}

      {groups.length === 0 && !showPlan ? (
        <div className="rounded-lg border border-line bg-bg-1 px-5 py-8 text-center shadow-[var(--shadow-card)]">
          <p className="text-fg-1 font-medium">No favorites picked yet.</p>
          <p className="text-fg-3 text-sm mt-1">
            Pick rides on the <a href="/me" className="underline">My alerts</a>{" "}
            page and they&rsquo;ll show here with live waits.
          </p>
        </div>
      ) : (
        <div className="space-y-8">
          {groups.map((g) => (
            <section key={g.park_key}>
              <h3 className="label-meta mb-2">{g.park_name}</h3>
              <div className="rounded-lg border border-line bg-bg-1 shadow-[var(--shadow-card)] divide-y divide-line-soft">
                {g.rides.map((r) => (
                  <WaitRow key={r.ride_id} ride={r} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      <footer className="mt-10 space-y-3">
        {waits.updated_at && (
          <p className="text-fg-3 text-xs">
            As of {new Date(waits.updated_at).toLocaleTimeString("en-US", {
              timeZone: "America/New_York",
              hour: "numeric",
              minute: "2-digit",
            })}{" "}
            ET · refreshes every minute
          </p>
        )}
        <details className="text-xs text-fg-3">
          <summary className="cursor-pointer">Phone widget setup</summary>
          <div className="mt-2 space-y-2">
            <p>
              Your private feed URL (treat it like a password — anyone with
              it can see these waits):
            </p>
            <code className="block break-all rounded bg-bg-2 p-2 select-all">
              {`https://magicmonitor.megillini.dev/api/widget/waits?t=${sub}.${widgetSecret}`}
            </code>
            <p>
              Paste it into the Scriptable script at{" "}
              <code>tools/widget/scriptable-waits.js</code> in the repo to get
              a home-screen widget.
            </p>
          </div>
        </details>
      </footer>
    </div>
  );
}

function Chip({ href, active, label }: { href: string; active: boolean; label: string }) {
  return (
    <a
      href={href}
      className={
        "rounded-full px-3 py-1 text-xs font-medium border " +
        (active
          ? "border-gold/60 bg-gold/15 text-fg-0"
          : "border-line bg-bg-1 text-fg-2")
      }
    >
      {label}
    </a>
  );
}

function WaitRow({ ride, ordinal }: { ride: MyWaitRide; ordinal?: number }) {
  const down = ride.status === "DOWN";
  const operating = ride.status === "OPERATING";
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2.5">
      <p className="text-fg-0 text-sm font-medium truncate">
        {ordinal !== undefined && (
          <span className="text-fg-3 mr-2">{ordinal}.</span>
        )}
        {ride.ride_name}
      </p>
      {down ? (
        <span className="shrink-0 rounded-full bg-bad/15 px-2.5 py-0.5 text-xs font-medium text-bad">
          Down
        </span>
      ) : operating && ride.wait_mins !== null ? (
        <span className="shrink-0 text-fg-0 font-semibold tabular-nums">
          {ride.wait_mins}
          <span className="text-fg-3 text-xs font-normal ml-0.5">min</span>
        </span>
      ) : (
        <span className="shrink-0 text-fg-3 text-xs">
          {ride.status === "OPERATING" ? "Open" : ride.status.toLowerCase()}
        </span>
      )}
    </div>
  );
}
