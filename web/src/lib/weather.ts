/**
 * Current conditions at WDW for the /waits page + widget feed.
 *
 * Same Open-Meteo endpoint + coords the poller's weather module uses
 * (infra/lambda/poller/weather.py) — free, no API key. One property-wide
 * reading: the four parks sit within ~5 miles, so per-park weather would
 * be false precision. Cached for 5 minutes via Next's fetch cache so the
 * page/widget can't hammer the API (Open-Meteo updates ~every 15 min).
 */
import "server-only";

const WDW_LAT = 28.3852;
const WDW_LON = -81.5639;

export interface CurrentConditions {
  temp_f: number;
  /** Human label derived from the WMO weather code ("Clear", "Rain"…). */
  condition: string;
  /** Glanceable emoji for the widget/title row. */
  icon: string;
  is_raining: boolean;
}

/** WMO weather-code buckets → label + emoji. Coarse on purpose — this is
 *  a glance widget, not a forecast product. Thunderstorm codes match the
 *  poller's _STORM_CODES (95/96/99). */
function describe(code: number): { condition: string; icon: string } {
  if (code < 0 || !Number.isFinite(code)) return { condition: "—", icon: "🌡️" };
  if (code === 0) return { condition: "Clear", icon: "☀️" };
  if (code <= 2) return { condition: "Partly cloudy", icon: "⛅" };
  if (code === 3) return { condition: "Overcast", icon: "☁️" };
  if (code === 45 || code === 48) return { condition: "Fog", icon: "🌫️" };
  if (code <= 57) return { condition: "Drizzle", icon: "🌦️" };
  if (code <= 67) return { condition: "Rain", icon: "🌧️" };
  if (code <= 77) return { condition: "Sleet", icon: "🌨️" };
  if (code <= 82) return { condition: "Showers", icon: "🌧️" };
  if (code >= 95) return { condition: "Thunderstorm", icon: "⛈️" };
  return { condition: "—", icon: "🌡️" };
}

/** Exported for tests. */
export const _describe = describe;

export async function getCurrentConditions(): Promise<CurrentConditions | null> {
  try {
    const url =
      "https://api.open-meteo.com/v1/forecast" +
      `?latitude=${WDW_LAT}&longitude=${WDW_LON}` +
      "&current=temperature_2m,precipitation,weather_code" +
      "&temperature_unit=fahrenheit&timezone=America%2FNew_York";
    const resp = await fetch(url, { next: { revalidate: 300 } });
    if (!resp.ok) return null;
    const data = (await resp.json()) as {
      current?: { temperature_2m?: number; precipitation?: number; weather_code?: number };
    };
    const cur = data.current;
    if (cur?.temperature_2m === undefined) return null;
    const code = cur.weather_code ?? -1;
    const { condition, icon } = describe(code);
    return {
      temp_f: Math.round(cur.temperature_2m),
      condition,
      icon,
      is_raining: (cur.precipitation ?? 0) > 0 || (code >= 51 && code <= 99),
    };
  } catch {
    // Weather is garnish — the waits page must render without it.
    return null;
  }
}
