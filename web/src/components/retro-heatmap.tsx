import {
  DOW_LABELS,
  type HeatmapCell,
} from "@/lib/analytics";
import {
  DISPLAY_HOURS,
  HEAT_CLOSED,
  HEAT_SCALE,
  formatHour,
  heatColor,
} from "@/lib/heat";

/**
 * Hour × day-of-week heatmap in the poster style: fixed 22px cells,
 * 3px gaps, five-step calm→packed scale, closed hours as flat
 * parchment cells. 20 hour columns (7a–2a in park-day order) × 7 day
 * rows with a 38px day-label gutter.
 *
 * Used twice: the home-page teaser (no hour labels / no legend) and
 * the per-park analytics page (both on). Intensity is relative to the
 * park's own max so each park's heatmap is self-contained.
 */
export function RetroHeatmap({
  cells,
  showHourLabels = false,
  showLegend = false,
}: {
  cells: HeatmapCell[];
  showHourLabels?: boolean;
  showLegend?: boolean;
}) {
  if (cells.length === 0) {
    return <p className="text-fg-2 text-sm">No data.</p>;
  }
  const maxWait = Math.max(...cells.map((c) => c.wait), 1);

  // 7 × 24 grid from the sparse cell list; cells missing where the
  // park is closed.
  const grid: (HeatmapCell | null)[][] = Array.from({ length: 7 }, () =>
    Array(24).fill(null),
  );
  for (const c of cells) {
    if (c.dow >= 0 && c.dow < 7 && c.hour >= 0 && c.hour < 24) {
      grid[c.dow][c.hour] = c;
    }
  }

  return (
    <div className="overflow-x-auto">
      <div className="w-fit">
        {showHourLabels && (
          <div className="mb-1 flex gap-[3px]">
            <div className="w-[38px] shrink-0" />
            {DISPLAY_HOURS.map((h, i) => (
              <div
                key={i}
                className="w-[22px] shrink-0 text-center font-head font-semibold text-[10px] text-fg-3"
              >
                {i % 3 === 0 ? formatHour(h) : ""}
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-col gap-[3px]">
          {DOW_LABELS.map((label, dow) => (
            <div key={dow} className="flex items-center gap-[3px]">
              <div
                className="w-[38px] shrink-0 font-head font-semibold text-[11px] text-fg-0"
                style={{ letterSpacing: "0.08em" }}
              >
                {label}
              </div>
              {DISPLAY_HOURS.map((h, i) => {
                const cell = grid[dow][h];
                if (!cell) {
                  // No data — park closed for this (dow, hour).
                  return (
                    <div
                      key={i}
                      className="h-[22px] w-[22px] shrink-0 rounded-[3px]"
                      style={{ background: HEAT_CLOSED }}
                    />
                  );
                }
                return (
                  <div
                    key={i}
                    className="h-[22px] w-[22px] shrink-0 rounded-[3px]"
                    style={{ background: heatColor(cell.wait / maxWait) }}
                    title={`${label} ${formatHour(h)} — avg ${cell.wait} min`}
                  />
                );
              })}
            </div>
          ))}
        </div>

        {showLegend && (
          <div className="mt-3.5 flex items-center gap-2">
            <span className="label-meta">Calm</span>
            {HEAT_SCALE.map((c) => (
              <div
                key={c}
                className="h-3 w-[18px] rounded-sm"
                style={{ background: c }}
              />
            ))}
            <span className="label-meta">Packed</span>
          </div>
        )}
      </div>
    </div>
  );
}
