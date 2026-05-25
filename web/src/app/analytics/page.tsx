import Link from "next/link";

import {
  formatDateRange,
  formatPollCount,
  getAnalytics,
  getRidesForPark,
} from "@/lib/analytics";
import { PARKS } from "@/lib/parks";

// Static — the snapshot is checked into the repo, no per-request work.
// Marking force-static keeps Amplify SSR from booting a Lambda for what
// is effectively a generated-at-build-time page.
export const dynamic = "force-static";

export default function AnalyticsPage() {
  const data = getAnalytics();

  return (
    <div className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-10 max-w-2xl">
        <p className="label-meta">Analytics · {formatPollCount(data.total_polls)} polls</p>
        <h2 className="display text-4xl font-medium mt-2">
          The patterns behind the wait times.
        </h2>
        <p className="text-fg-2 mt-3 leading-relaxed">
          Aggregated from{" "}
          <span className="text-fg-0">
            {formatDateRange(data.date_range.start, data.date_range.end)}
          </span>{" "}
          of polling history. Pick a park for an hour-by-day heatmap and the
          most-down rides in the data window.
        </p>
      </header>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {PARKS.map((park) => (
          <ParkSummaryCard key={park.key} parkKey={park.key} parkName={park.name} accentVar={park.accentVar} />
        ))}
      </section>

      <p className="label-meta mt-12 max-w-2xl leading-relaxed">
        Snapshot from {data.date_range.end.slice(0, 10)}. Sourced from
        DynamoDB.
      </p>
    </div>
  );
}

function ParkSummaryCard({
  parkKey,
  parkName,
  accentVar,
}: {
  parkKey: import("@/lib/parks").ParkKey;
  parkName: string;
  accentVar: string;
}) {
  const rides = getRidesForPark(parkKey);
  if (rides.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-bg-1 px-6 py-5 shadow-[var(--shadow-card)]">
        <h3 className="display text-2xl font-medium text-fg-0">{parkName}</h3>
        <p className="text-fg-2 text-sm mt-3">No analytics data for this park.</p>
      </div>
    );
  }

  const avgDowntime =
    rides.reduce((s, r) => s + r.downtime_pct, 0) / rides.length;
  // Already sorted by downtime desc in the snapshot, but rides[] in the
  // snapshot is the global list — we filtered to this park, sort order
  // preserved.
  const mostDown = rides[0];
  const operatingRides = rides.filter((r) => r.avg_wait !== null);
  const avgWait =
    operatingRides.length > 0
      ? operatingRides.reduce((s, r) => s + (r.avg_wait ?? 0), 0) /
        operatingRides.length
      : null;

  return (
    <Link
      href={`/parks/${parkKey}/analytics`}
      className="group relative flex items-stretch overflow-hidden rounded-lg border border-line bg-bg-1 hover:bg-bg-2 transition-colors shadow-[var(--shadow-card)]"
      style={{ "--park-accent": `var(${accentVar})` } as React.CSSProperties}
    >
      <div className="w-2 shrink-0" style={{ background: "var(--park-accent)" }} />
      <div className="flex-1 px-6 py-5">
        <h3 className="display text-2xl font-medium text-fg-0">{parkName}</h3>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <div>
            <dt className="label-meta">Avg downtime</dt>
            <dd className="text-fg-0 mt-0.5">
              <span className="display text-xl font-medium tabular-nums">
                {avgDowntime.toFixed(1)}
              </span>
              <span className="label-meta ml-1">%</span>
            </dd>
          </div>
          <div>
            <dt className="label-meta">Avg wait</dt>
            <dd className="text-fg-0 mt-0.5">
              <span className="display text-xl font-medium tabular-nums">
                {avgWait !== null ? Math.round(avgWait) : "—"}
              </span>
              <span className="label-meta ml-1">min</span>
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="label-meta">Most down</dt>
            <dd className="text-fg-1 text-sm mt-0.5 truncate">
              {mostDown.ride_name}{" "}
              <span className="label-meta tabular-nums">
                ({mostDown.downtime_pct.toFixed(1)}%)
              </span>
            </dd>
          </div>
        </dl>
        <p className="mt-4 text-xs text-fg-3 group-hover:text-fg-2 transition-colors">
          See heatmap + ride breakdown →
        </p>
      </div>
    </Link>
  );
}
