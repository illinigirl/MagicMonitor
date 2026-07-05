/**
 * Retro heatmap color scale — the poster design's five-step ramp from
 * calm sage to packed red-orange, plus the closed-hours cell color.
 * Shared by the home-page teaser and the per-park analytics heatmap.
 */

export const HEAT_SCALE = [
  "#DDEAE3", // calm
  "#9CC9B4",
  "#E8C05A",
  "#E07B39",
  "#CE3F2A", // packed
] as const;

export const HEAT_CLOSED = "#EFE7D2";

/** Map a 0..1 intensity onto one of the five scale steps. */
export function heatColor(intensity: number): string {
  const t = Math.min(1, Math.max(0, intensity));
  return HEAT_SCALE[Math.min(HEAT_SCALE.length - 1, Math.floor(t * HEAT_SCALE.length))];
}

/**
 * Display hour order: 7am through 2am in park-day flow (after-midnight
 * hours belong to the previous park day, matching the aggregator's dow
 * assignment). 20 columns, per the design.
 */
export const DISPLAY_HOURS = [
  7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2,
];

export function formatHour(h: number): string {
  if (h === 0) return "12a";
  if (h === 12) return "12p";
  return h < 12 ? `${h}a` : `${h - 12}p`;
}
