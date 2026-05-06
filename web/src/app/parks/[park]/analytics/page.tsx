import { notFound } from "next/navigation";
import Link from "next/link";

import {
  DOW_LABELS,
  formatDateRange,
  getAnalytics,
  getParkHeatmap,
  getRidesForPark,
  type HeatmapCell,
  type RideAnalytics,
} from "@/lib/analytics";
import { findPark } from "@/lib/parks";

export const dynamic = "force-static";

export default async function ParkAnalyticsPage({
  params,
}: {
  params: Promise<{ park: string }>;
}) {
  const { park: parkKeyRaw } = await params;
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  const data = getAnalytics();
  const rides = getRidesForPark(park.key);
  const heatmap = getParkHeatmap(park.key);

  return (
    <div
      className="mx-auto max-w-5xl px-6 py-12"
      style={{ "--park-accent": `var(${park.accentVar})` } as React.CSSProperties}
    >
      <header className="mb-10">
        <Link
          href="/analytics"
          className="text-fg-3 hover:text-fg-1 text-sm transition-colors"
        >
          ← All parks · analytics
        </Link>
        <div className="flex items-baseline gap-3 mt-3 flex-wrap">
          <h2 className="display text-4xl font-medium">{park.name}</h2>
          <span
            className="h-1 w-12 rounded-full"
            style={{ background: "var(--park-accent)" }}
          />
        </div>
        <p className="text-fg-2 mt-2">
          {formatDateRange(data.date_range.start, data.date_range.end)} ·
          {" "}
          {rides.length} attractions in the data window
        </p>
      </header>

      <section className="mb-12">
        <h3 className="display text-xl font-medium mb-2 text-fg-1">
          Hour × day-of-week heatmap
        </h3>
        <p className="text-fg-2 text-sm mb-5 max-w-2xl leading-relaxed">
          Average reported wait across all attractions, bucketed by hour of
          day (Eastern) and day of week. Brighter cells = longer waits. Park
          closing hours show as gaps.
        </p>
        <Heatmap cells={heatmap} />
      </section>

      <section>
        <h3 className="display text-xl font-medium mb-2 text-fg-1">
          Ride downtime ranking
        </h3>
        <p className="text-fg-2 text-sm mb-5 max-w-2xl leading-relaxed">
          Percentage of operating-window polls each ride spent in DOWN status.
          Higher = more breakdowns / mid-day shutdowns over the data window.
        </p>
        <RideTable rides={rides} />
      </section>
    </div>
  );
}

/**
 * Heatmap as a CSS grid: 25 columns (label + 24 hours), 8 rows (header
 * + 7 days). One cell per (dow, hour). Color intensity scales with the
 * cell's wait relative to the park's max — keeps each park's heatmap
 * self-contained rather than normalizing across parks (different parks
 * have very different baseline waits).
 */
function Heatmap({ cells }: { cells: HeatmapCell[] }) {
  if (cells.length === 0) {
    return <p className="text-fg-2 text-sm">No data.</p>;
  }
  const maxWait = Math.max(...cells.map((c) => c.wait), 1);

  // 7 × 24 grid, populated from the (sparse) cells list. Some
  // (dow, hour) cells will be missing where the park is closed.
  const grid: (HeatmapCell | null)[][] = Array.from({ length: 7 }, () =>
    Array(24).fill(null),
  );
  for (const c of cells) {
    if (c.dow >= 0 && c.dow < 7 && c.hour >= 0 && c.hour < 24) {
      grid[c.dow][c.hour] = c;
    }
  }

  return (
    <div className="overflow-x-auto -mx-6 px-6">
      <div
        className="grid gap-1 min-w-[640px]"
        style={{ gridTemplateColumns: "auto repeat(24, minmax(0,1fr))" }}
      >
        {/* Header row: empty corner + 24 hour labels. Show every 3rd
            label so the row doesn't get cluttered at small widths. */}
        <div />
        {Array.from({ length: 24 }).map((_, h) => (
          <div
            key={h}
            className="text-fg-3 text-[10px] tabular-nums text-center"
          >
            {h % 3 === 0 ? formatHour(h) : ""}
          </div>
        ))}

        {DOW_LABELS.map((label, dow) => (
          <FragmentRow
            key={dow}
            label={label}
            cells={grid[dow]}
            maxWait={maxWait}
          />
        ))}
      </div>
      <Legend maxWait={maxWait} />
    </div>
  );
}

function FragmentRow({
  label,
  cells,
  maxWait,
}: {
  label: string;
  cells: (HeatmapCell | null)[];
  maxWait: number;
}) {
  return (
    <>
      <div className="text-fg-2 text-xs pr-2 self-center">{label}</div>
      {cells.map((cell, h) => (
        <HeatCell key={h} cell={cell} maxWait={maxWait} hour={h} dow={label} />
      ))}
    </>
  );
}

function HeatCell({
  cell,
  maxWait,
  hour,
  dow,
}: {
  cell: HeatmapCell | null;
  maxWait: number;
  hour: number;
  dow: string;
}) {
  if (!cell) {
    // No data for this (dow, hour) — usually park closed.
    return <div className="aspect-square rounded-sm bg-bg-2/30" />;
  }
  const intensity = Math.min(1, cell.wait / maxWait);
  return (
    <div
      className="aspect-square rounded-sm"
      style={{ background: heatmapColor(intensity) }}
      title={`${dow} ${formatHour(hour)} — avg ${cell.wait} min`}
    />
  );
}

/**
 * Map intensity (0..1) onto an OKLCH gradient from a dim background
 * navy to gold. Keeps the heatmap on-palette without introducing new
 * tokens. We push lightness + chroma together so dim cells are subtle
 * and bright cells pop.
 */
function heatmapColor(t: number): string {
  const clamped = Math.min(1, Math.max(0, t));
  const l = 0.27 + clamped * 0.55; // 0.27 (~bg-2) → 0.82 (~gold)
  const c = 0.04 + clamped * 0.13; // dim → saturated
  const h = 260 - clamped * 175;   // navy hue → gold hue
  return `oklch(${l.toFixed(2)} ${c.toFixed(2)} ${h.toFixed(0)})`;
}

function formatHour(h: number): string {
  if (h === 0) return "12a";
  if (h === 12) return "12p";
  return h < 12 ? `${h}a` : `${h - 12}p`;
}

function Legend({ maxWait }: { maxWait: number }) {
  return (
    <div className="mt-4 flex items-center gap-3 text-xs text-fg-3">
      <span>0 min</span>
      <div
        className="h-2 w-40 rounded-sm"
        style={{
          background:
            "linear-gradient(to right, oklch(0.27 0.04 260), oklch(0.55 0.10 170), oklch(0.82 0.17 85))",
        }}
      />
      <span className="tabular-nums">{Math.round(maxWait)} min</span>
    </div>
  );
}

function RideTable({ rides }: { rides: RideAnalytics[] }) {
  // Highest possible downtime in this park, used to scale the bars.
  const maxDowntime = Math.max(...rides.map((r) => r.downtime_pct), 1);

  return (
    <div className="rounded-lg border border-line bg-bg-1 shadow-[var(--shadow-card)] overflow-hidden">
      {rides.map((r) => (
        <RideRow key={r.ride_id} ride={r} maxDowntime={maxDowntime} />
      ))}
    </div>
  );
}

function RideRow({
  ride,
  maxDowntime,
}: {
  ride: RideAnalytics;
  maxDowntime: number;
}) {
  const barWidth = Math.max(2, (ride.downtime_pct / maxDowntime) * 100);
  return (
    <div className="border-b border-line-soft last:border-b-0 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-fg-0 font-medium truncate">{ride.ride_name}</span>
        <span className="display text-base text-fg-0 tabular-nums shrink-0">
          {ride.downtime_pct.toFixed(1)}
          <span className="label-meta ml-1">%</span>
        </span>
      </div>
      <div className="mt-1.5 h-1.5 rounded-full bg-bg-2 overflow-hidden">
        <div
          className="h-full rounded-full transition-all"
          style={{
            width: `${barWidth}%`,
            background: "var(--park-accent)",
          }}
        />
      </div>
      <div className="mt-1 flex items-center gap-3 label-meta">
        <span>
          avg wait{" "}
          <span className="text-fg-2 tabular-nums">
            {ride.avg_wait ?? "—"}
          </span>
          {ride.avg_wait !== null && " min"}
        </span>
        <span>·</span>
        <span>
          peak{" "}
          <span className="text-fg-2 tabular-nums">{ride.max_wait ?? "—"}</span>
          {ride.max_wait !== null && " min"}
        </span>
        <span>·</span>
        <span className="tabular-nums">
          {ride.total_polls.toLocaleString()} polls
        </span>
      </div>
    </div>
  );
}
