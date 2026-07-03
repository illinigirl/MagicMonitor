// Magic Monitor — iOS home-screen waits widget (Scriptable).
//
// Setup (one time):
//   1. Install "Scriptable" from the App Store.
//   2. New script → paste this file → name it "MM Waits".
//   3. Sign into magicmonitor.megillini.dev/waits on your phone, expand
//      "Phone widget setup", copy your private feed URL, and paste it
//      into FEED_URL below. Treat that URL like a password.
//   4. Long-press home screen → add a Scriptable widget (medium works
//      best) → choose "MM Waits".
//
// Shows: today's active plan (if any) or your favorites, DOWN rides
// flagged, plus current temp/conditions at WDW. iOS refreshes widgets on
// its own cadence (~every 5–15 min); tap the widget to open /waits for
// live-now numbers.

const FEED_URL = "PASTE_YOUR_FEED_URL_HERE";

const MAX_ROWS = 6;

async function run() {
  const w = new ListWidget();
  w.backgroundColor = new Color("#141210");
  w.url = "https://magicmonitor.megillini.dev/waits";
  w.setPadding(12, 14, 12, 14);

  let data;
  try {
    data = await new Request(FEED_URL).loadJSON();
  } catch (e) {
    w.addText("Magic Monitor").font = Font.boldSystemFont(12);
    const err = w.addText("Feed unreachable — tap to open");
    err.font = Font.systemFont(10);
    err.textColor = Color.gray();
    return finish(w);
  }

  // Header: title + weather.
  const head = w.addStack();
  head.centerAlignContent();
  const title = head.addText("Magic Monitor");
  title.font = Font.boldSystemFont(12);
  title.textColor = new Color("#d4af37");
  head.addSpacer();
  if (data.weather) {
    const wx = head.addText(
      `${data.weather.icon} ${data.weather.temp_f}°`,
    );
    wx.font = Font.systemFont(12);
    wx.textColor = Color.white();
  }
  w.addSpacer(6);

  // Prefer the active plan (it's "what's next"); fall back to favorites.
  let rows = [];
  if (data.plan && data.plan.rides.length > 0) {
    rows = data.plan.rides.map((r, i) => ({ ...r, prefix: `${i + 1}. ` }));
  } else {
    rows = (data.parks ?? []).flatMap((g) => g.rides);
  }

  if (rows.length === 0) {
    const none = w.addText("No favorites picked yet — tap to set up.");
    none.font = Font.systemFont(11);
    none.textColor = Color.gray();
    return finish(w);
  }

  for (const r of rows.slice(0, MAX_ROWS)) {
    const line = w.addStack();
    line.centerAlignContent();
    const name = line.addText(`${r.prefix ?? ""}${r.ride_name}`);
    name.font = Font.systemFont(11);
    name.textColor = Color.white();
    name.lineLimit = 1;
    line.addSpacer();
    let right, color;
    if (r.status === "DOWN") {
      right = "DOWN";
      color = new Color("#e05d4b");
    } else if (r.status === "OPERATING" && r.wait_mins !== null) {
      right = `${r.wait_mins}m`;
      color = r.wait_mins <= 20 ? new Color("#7dc47d") : Color.white();
    } else {
      right = r.status.toLowerCase();
      color = Color.gray();
    }
    const val = line.addText(right);
    val.font = Font.boldMonospacedSystemFont(11);
    val.textColor = color;
    w.addSpacer(3);
  }

  return finish(w);
}

function finish(w) {
  if (config.runsInWidget) {
    Script.setWidget(w);
  } else {
    w.presentMedium();
  }
  Script.complete();
}

await run();
