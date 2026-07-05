import { getCurrentConditions } from "@/lib/weather";

/**
 * Current-conditions chip in the poster style: bordered Oswald-caps
 * label with the glance emoji, e.g. "⛈️ ORLANDO · 91° · THUNDERSTORM".
 *
 * Async server component — drop it into any server page. The fetch
 * behind getCurrentConditions is cached 5 min (Next fetch cache), so
 * stacking it on several pages costs one upstream call per window.
 * Weather is garnish: on fetch failure it renders nothing.
 *
 * Rain/storm conditions switch the chip to the red-orange alert
 * treatment — wet weather is exactly when outdoor rides start going
 * DOWN, so the chip doubles as a "that's why" hint next to a wall of
 * down rides.
 */
export async function WeatherChip() {
  const wx = await getCurrentConditions();
  if (!wx) return null;

  const alert = wx.is_raining || wx.condition === "Thunderstorm";

  return (
    <span
      className={`inline-flex items-center gap-2 rounded-[5px] border-2 bg-bg-1 px-3 py-1.5 font-head font-semibold text-[11px] uppercase tracking-[0.14em] ${
        alert ? "border-accent text-accent" : "border-line text-fg-0"
      }`}
      title="Current conditions at Walt Disney World (updates ~every 15 min)"
    >
      <span aria-hidden>{wx.icon}</span>
      Orlando · {wx.temp_f}° · {wx.condition}
    </span>
  );
}
