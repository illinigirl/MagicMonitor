/**
 * Tap-to-navigate links for plan entries. Rides with known coordinates
 * (attraction-locations.json — KEEP IN SYNC with mcp/data/, same rule
 * as the planner) get Apple Maps WALKING directions straight to the
 * ride; meals/shows/unknown rides fall back to a Maps search of the
 * name pinned to the park. maps.apple.com universal links open the
 * native Maps app on iOS (the family's platform) and the web UI
 * elsewhere — no per-platform branching needed.
 */
import locations from "@/data/attraction-locations.json";

const byId = locations as Record<
  string,
  { name: string; park_key: string; lat: number; lon: number }
>;

export function mapsUrl(opts: {
  ride_id?: string | null;
  name: string;
  parkName?: string;
  /** Android UA → Google Maps URLs (app-links into the native app);
   *  everything else → Apple Maps (native on the household iPhones). */
  android?: boolean;
}): string {
  const loc = opts.ride_id ? byId[opts.ride_id] : undefined;
  if (loc) {
    return opts.android
      ? `https://www.google.com/maps/dir/?api=1&destination=${loc.lat},${loc.lon}&travelmode=walking`
      : `https://maps.apple.com/?daddr=${loc.lat},${loc.lon}&dirflg=w`;
  }
  const q = encodeURIComponent(
    `${opts.name} ${opts.parkName ?? "Walt Disney World"}`,
  );
  return opts.android
    ? `https://www.google.com/maps/search/?api=1&query=${q}`
    : `https://maps.apple.com/?q=${q}`;
}

/** Android per the request's User-Agent (SSR pages pass headers()). */
export function isAndroidUa(ua: string | null | undefined): boolean {
  return /android/i.test(ua ?? "");
}
