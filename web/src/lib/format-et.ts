/**
 * Wall-clock display for plan timestamps ("2026-07-04T14:30:00-04:00"
 * → "2:30 PM"). The stored offset IS park time (every writer — MCP,
 * /replan, the poller — stamps ET), so render the digits as-is rather
 * than converting through the server's timezone.
 */
export function formatEtTime(iso: string): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return "";
  const h24 = Number(m[1]);
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${m[2]} ${h24 < 12 ? "AM" : "PM"}`;
}
