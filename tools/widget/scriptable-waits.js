// Magic Monitor — iOS home-screen waits widget (Scriptable).
//
// Shows ONE park's waits (a home-screen widget shouldn't try to be the
// whole park map). Which park:
//   1. If you have an ACTIVE PLAN today → that park (auto — the "right
//      park for the day", no config).
//   2. Else the park you PIN via the widget Parameter (see setup #5).
//   3. Else your park with the most favorites (never blank).
// Plus DOWN rides flagged and current temp/conditions at WDW.
//
// Setup (one time):
//   1. Install "Scriptable" from the App Store.
//   2. New script → paste this file → name it "MM Waits".
//   3. Sign into magicmonitor.megillini.dev/waits on your phone, expand
//      "Phone widget setup", copy your private feed URL, and paste it
//      into FEED_URL below. Treat that URL like a password.
//   4. Long-press home screen → add a Scriptable widget → choose
//      "MM Waits". Small fits ~5 rides; medium/large fit more.
//   5. (Optional) Pin a park: long-press the widget → Edit Widget →
//      Parameter → type a park (epcot, MK, "hollywood studios", AK…).
//      Leave it blank to auto-follow today's plan.
//
// iOS refreshes widgets on its own cadence (~5–15 min); tap to open
// /waits for live-now numbers.

const FEED_URL = "PASTE_YOUR_FEED_URL_HERE";

// How many ride rows fit, by widget size.
function rideBudget() {
  switch (config.widgetFamily) {
    case "small": return 5;
    case "large": return 20;
    default: return 10; // medium (and the in-app preview)
  }
}

// Loose match of a pinned-park string to a feed park group. Accepts the
// key (magic_kingdom), the name (Magic Kingdom), or a short code (MK).
const SHORT_CODES = {
  mk: "magic_kingdom",
  ep: "epcot",
  epcot: "epcot",
  hs: "hollywood_studios",
  dhs: "hollywood_studios",
  ak: "animal_kingdom",
};
function matchPark(groups, raw) {
  if (!raw) return null;
  const q = raw.trim().toLowerCase();
  const code = SHORT_CODES[q];
  return (
    groups.find((g) => g.park_key === q) ||
    groups.find((g) => g.park_name.toLowerCase() === q) ||
    (code && groups.find((g) => g.park_key === code)) ||
    groups.find((g) => g.park_name.toLowerCase().includes(q)) ||
    null
  );
}

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

  // Decide the single park + its rides. Plan wins; then pinned param;
  // then largest favorites group.
  const planActive = data.plan && data.plan.rides.length > 0;
  const groups = data.parks ?? [];
  let parkLabel, rides, planning = false;

  if (planActive) {
    planning = true;
    parkLabel = data.plan.park_name;
    rides = data.plan.rides.map((r, i) => ({ ...r, prefix: `${i + 1}. ` }));
  } else {
    const pinned = matchPark(groups, args.widgetParameter);
    const group =
      pinned ||
      [...groups].sort((a, b) => b.rides.length - a.rides.length)[0];
    if (group) {
      parkLabel = group.park_name;
      rides = group.rides;
    }
  }

  // Header: park (or brand) on the left, weather on the right.
  const head = w.addStack();
  head.centerAlignContent();
  const title = head.addText(parkLabel || "Magic Monitor");
  title.font = Font.boldSystemFont(13);
  title.textColor = new Color("#d4af37");
  title.lineLimit = 1;
  head.addSpacer();
  if (data.weather) {
    const wx = head.addText(`${data.weather.icon} ${data.weather.temp_f}°`);
    wx.font = Font.systemFont(12);
    wx.textColor = Color.white();
  }
  if (planning) {
    const sub = w.addText("Today's plan");
    sub.font = Font.mediumSystemFont(9);
    sub.textColor = new Color("#8a8378");
  }
  w.addSpacer(6);

  if (!rides || rides.length === 0) {
    const none = w.addText(
      groups.length === 0
        ? "No favorites picked yet — tap to set up."
        : "No rides for that park.",
    );
    none.font = Font.systemFont(11);
    none.textColor = Color.gray();
    return finish(w);
  }

  const budget = rideBudget();
  for (const r of rides.slice(0, budget)) {
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

  // Show when we've trimmed, so a hidden ride isn't a silent surprise.
  if (rides.length > budget) {
    w.addSpacer(2);
    const more = w.addText(`+${rides.length - budget} more — tap`);
    more.font = Font.systemFont(9);
    more.textColor = new Color("#8a8378");
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
