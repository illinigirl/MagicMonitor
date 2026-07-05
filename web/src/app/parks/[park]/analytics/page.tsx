import { notFound } from "next/navigation";
import Link from "next/link";

import {
  formatDateRange,
  getAnalytics,
  getParkHeatmap,
  getRidesForPark,
  type RideAnalytics,
} from "@/lib/analytics";
import { RetroHeatmap } from "@/components/retro-heatmap";
import { heatColor } from "@/lib/heat";
import { findPark } from "@/lib/parks";

// Default rendering (no force-static) — the snapshot is in-memory at
// module load, so per-request work is just the sort + render. We need
// search params to drive ?sort=, which force-static strips.
const SORT_MODES = ["down", "wait", "name"] as const;
type SortMode = (typeof SORT_MODES)[number];

function parseSort(raw: string | undefined): SortMode {
  return SORT_MODES.includes(raw as SortMode) ? (raw as SortMode) : "down";
}

export default async function ParkAnalyticsPage({
  params,
  searchParams,
}: {
  params: Promise<{ park: string }>;
  searchParams: Promise<{ sort?: string }>;
}) {
  const [{ park: parkKeyRaw }, sp] = await Promise.all([params, searchParams]);
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  const sort = parseSort(sp.sort);
  const data = getAnalytics();
  const rides = sortRides(getRidesForPark(park.key), sort);
  const heatmap = getParkHeatmap(park.key);

  return (
    <div className="mx-auto max-w-5xl px-6 md:px-10 pb-4">
      <header className="pt-9">
        <Link href="/analytics" className="kicker hover:underline">
          ← All parks · Analytics
        </Link>
        <div className="mt-3 flex flex-wrap items-baseline gap-[18px]">
          <h2 className="display text-4xl md:text-[54px] leading-[1.05] text-fg-0 uppercase">
            {park.name}
          </h2>
          <span
            className="h-[5px] w-16 -translate-y-2 bg-accent"
            aria-hidden
          />
        </div>
        <p className="mt-2.5 text-[15px] text-fg-2">
          {formatDateRange(data.date_range.start, data.date_range.end)} ·{" "}
          {rides.length} attractions in the data window
        </p>
      </header>

      <section className="mt-8 border-t-2 border-line pt-6">
        <h3 className="head text-xl">Hour × day-of-week heatmap</h3>
        <p className="mb-4 mt-1.5 max-w-[700px] text-[13px] leading-relaxed text-fg-2">
          Average reported wait across all attractions, bucketed by hour of
          day (Eastern) and day of week. Darker red = longer waits. Park
          closing hours show as blank cells.
        </p>
        <RetroHeatmap cells={heatmap} showHourLabels showLegend />
      </section>

      <section className="mt-9 border-t-2 border-line pt-6">
        <h3 className="head text-xl">{SORT_HEADERS[sort]}</h3>
        <p className="mb-4 mt-1.5 max-w-2xl text-[13px] leading-relaxed text-fg-2">
          {SORT_DESCRIPTIONS[sort]}
        </p>
        <SortPills active={sort} parkKey={park.key} />
        <RideList rides={rides} sort={sort} />
      </section>
    </div>
  );
}

/**
 * Sort the per-park rides list by the requested mode. Nullable
 * fields (avg_wait can be null for never-operating rides in the
 * window) sort to the bottom regardless of direction so a refurb
 * ride doesn't leapfrog real data.
 */
function sortRides(rides: RideAnalytics[], mode: SortMode): RideAnalytics[] {
  const out = [...rides];
  if (mode === "down") {
    out.sort((a, b) => b.downtime_pct - a.downtime_pct);
  } else if (mode === "wait") {
    out.sort((a, b) => {
      if (a.avg_wait === null && b.avg_wait === null) return 0;
      if (a.avg_wait === null) return 1;
      if (b.avg_wait === null) return -1;
      return b.avg_wait - a.avg_wait;
    });
  } else {
    out.sort((a, b) => a.ride_name.localeCompare(b.ride_name));
  }
  return out;
}

const SORT_HEADERS: Record<SortMode, string> = {
  down: "Ride downtime ranking",
  wait: "Longest average waits",
  name: "All rides (A → Z)",
};

const SORT_DESCRIPTIONS: Record<SortMode, string> = {
  down:
    "Percentage of operating-window polls each ride spent in DOWN status. Higher = more breakdowns / mid-day shutdowns over the data window.",
  wait:
    "Average wait while operating. Useful for spotting which attractions consistently command the longest queues.",
  name:
    "Alphabetical. Useful when you want to look up a specific ride's stats without hunting.",
};

function SortPills({
  active,
  parkKey,
}: {
  active: SortMode;
  parkKey: import("@/lib/parks").ParkKey;
}) {
  const opts: { mode: SortMode; label: string }[] = [
    { mode: "down", label: "Most down" },
    { mode: "wait", label: "Longest wait" },
    { mode: "name", label: "A → Z" },
  ];
  return (
    <div className="mb-5 flex flex-wrap gap-2">
      {opts.map((o) => {
        // Default sort omits the param — keeps URLs clean and a
        // bookmark of the bare /analytics page lands on the same
        // default the page renders cold.
        const href =
          o.mode === "down"
            ? `/parks/${parkKey}/analytics`
            : `/parks/${parkKey}/analytics?sort=${o.mode}`;
        const isActive = o.mode === active;
        return (
          <Link
            key={o.mode}
            href={href}
            aria-current={isActive ? "page" : undefined}
            className={
              isActive
                ? "rounded-[5px] bg-accent px-3 py-1 font-head font-semibold text-[11px] uppercase tracking-[0.14em] text-bg-0"
                : "rounded-[5px] border-2 border-line bg-bg-1 px-3 py-1 font-head font-semibold text-[11px] uppercase tracking-[0.14em] text-fg-0 transition-colors duration-100 hover:border-accent hover:text-accent"
            }
          >
            {o.label}
          </Link>
        );
      })}
    </div>
  );
}

/**
 * Ride list in the poster bar-row style: Oswald-caps name column,
 * severity-colored bar on a parchment track, slab-serif headline
 * number in red-orange. The bar tracks whichever metric the user is
 * sorting by (name mode falls back to downtime so the bar isn't an
 * arbitrary signal); avg/peak/downtime fine print sits under the name.
 */
function RideList({ rides, sort }: { rides: RideAnalytics[]; sort: SortMode }) {
  const maxDowntime = Math.max(...rides.map((r) => r.downtime_pct), 1);
  const maxWait = Math.max(
    ...rides.map((r) => r.avg_wait ?? 0).filter((n) => n > 0),
    1,
  );
  return (
    <div className="flex flex-col gap-3">
      {rides.map((r) => (
        <RideRow
          key={r.ride_id}
          ride={r}
          maxDowntime={maxDowntime}
          maxWait={maxWait}
          sort={sort}
        />
      ))}
    </div>
  );
}

function RideRow({
  ride,
  maxDowntime,
  maxWait,
  sort,
}: {
  ride: RideAnalytics;
  maxDowntime: number;
  maxWait: number;
  sort: SortMode;
}) {
  const barMetric = sort === "wait" ? "wait" : "down";
  const intensity =
    barMetric === "wait" && ride.avg_wait !== null
      ? ride.avg_wait / maxWait
      : ride.downtime_pct / maxDowntime;
  const barWidth = Math.max(2, intensity * 100);
  const headline =
    sort === "wait"
      ? ride.avg_wait !== null
        ? `${ride.avg_wait} min`
        : "—"
      : `${ride.downtime_pct.toFixed(1)}%`;

  return (
    <div className="flex items-center gap-3.5">
      <div className="w-full sm:w-[250px] shrink-0">
        <div
          className="truncate font-head font-semibold text-sm uppercase text-fg-0"
          style={{ letterSpacing: "0.04em" }}
          title={ride.ride_name}
        >
          {ride.ride_name}
        </div>
        <div className="label-meta mt-0.5 !text-[10px] normal-case !tracking-wide">
          avg {ride.avg_wait ?? "—"}
          {ride.avg_wait !== null && "m"} · peak {ride.max_wait ?? "—"}
          {ride.max_wait !== null && "m"} · down{" "}
          {ride.downtime_pct.toFixed(1)}%
        </div>
      </div>
      <div className="hidden h-5 flex-1 overflow-hidden rounded-[3px] bg-bg-2 sm:block">
        <div
          className="h-full rounded-[3px]"
          style={{
            width: `${barWidth}%`,
            background: heatColor(intensity),
          }}
        />
      </div>
      <div className="display w-[70px] shrink-0 text-right text-[15px] text-accent">
        {headline}
      </div>
    </div>
  );
}
