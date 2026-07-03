// Magic Monitor — iOS home-screen waits widget (Scriptable).
//
// Shows ONE park's waits (a home-screen widget shouldn't try to be the
// whole park map). Which park:
//   1. A park PINNED via the widget Parameter always wins — a pinned
//      widget stays put (see setup #5).
//   2. Else, if you have an ACTIVE PLAN today → that park (a blank
//      widget auto-follows the plan — the "right park for the day").
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
//   5. (Optional) Pin a park: TAP this script inside Scriptable to open a
//      park picker that copies the code for you, OR just type one into
//      the widget Parameter (long-press widget → Edit Widget →
//      Parameter): epcot, MK, "hollywood studios", AK — matching is
//      forgiving. A pinned widget always stays on that park; leave it
//      blank to auto-follow today's plan. (Pin the parks you always
//      want to watch; keep one blank widget as your plan-of-the-day.)
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

// The four parks — the source of truth for both the picker and matching.
const PARKS = [
  { key: "magic_kingdom", code: "MK", name: "Magic Kingdom" },
  { key: "epcot", code: "EP", name: "EPCOT" },
  { key: "hollywood_studios", code: "HS", name: "Hollywood Studios" },
  { key: "animal_kingdom", code: "AK", name: "Animal Kingdom" },
];

// Loose match of a pinned-park string to a feed park group. Deliberately
// forgiving so you don't have to remember an exact abbreviation: the key
// (magic_kingdom), the code (MK), the name, or any distinctive substring
// (magic / hollywood / animal) all resolve.
function matchPark(groups, raw) {
  if (!raw) return null;
  const q = raw.trim().toLowerCase();
  if (!q) return null;
  const park =
    PARKS.find((p) => p.code.toLowerCase() === q) ||
    PARKS.find((p) => p.key === q) ||
    PARKS.find((p) => p.name.toLowerCase() === q) ||
    PARKS.find((p) => p.name.toLowerCase().includes(q) || q.includes(p.code.toLowerCase()));
  return park ? groups.find((g) => g.park_key === park.key) ?? null : null;
}

// Tap the script inside the Scriptable app → pick a park → its code is
// copied to the clipboard so you can paste it into the widget's
// Parameter field (iOS gives no dropdown there, so this is the "picker").
async function parkPicker() {
  const a = new Alert();
  a.title = "Pin a park to this widget";
  a.message =
    "Tap a park to copy its code, then: long-press the widget → " +
    "Edit Widget → Parameter → paste. Pick Auto to follow your plan.";
  PARKS.forEach((p) => a.addAction(`${p.name}  (${p.code})`));
  a.addAction("Auto — follow today's plan");
  a.addCancelAction("Cancel");
  const idx = await a.presentSheet();
  if (idx < 0) return;
  const code = idx < PARKS.length ? PARKS[idx].code : ""; // "" = Auto
  Pasteboard.copy(code);
  const done = new Alert();
  done.title = code ? `Copied "${code}"` : "Copied blank (Auto)";
  done.message =
    "Long-press the widget → Edit Widget → Parameter → paste" +
    (code ? "." : " (clears it so the widget follows your plan).");
  done.addAction("OK");
  await done.presentAlert();
}

async function run() {
  // Tapping the script in the Scriptable app opens the park picker
  // instead of rendering a preview — that's how you set the Parameter.
  if (!config.runsInWidget) {
    await parkPicker();
    return;
  }

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

  // Decide the single park + its rides. A PINNED param wins (a pinned
  // widget stays put); else today's active plan; else largest favorites.
  const groups = data.parks ?? [];
  const pinned = matchPark(groups, args.widgetParameter);
  const planActive = data.plan && data.plan.rides.length > 0;
  // A non-empty Parameter that resolved to nothing = a typo or a park
  // with no favorites; surface the valid codes rather than silently
  // falling back.
  const badParam = (args.widgetParameter || "").trim() && !pinned;
  let parkLabel, rides, planning = false;

  if (pinned) {
    parkLabel = pinned.park_name;
    rides = pinned.rides;
  } else if (planActive) {
    planning = true;
    parkLabel = data.plan.park_name;
    rides = data.plan.rides.map((r, i) => ({ ...r, prefix: `${i + 1}. ` }));
  } else {
    const group = [...groups].sort((a, b) => b.rides.length - a.rides.length)[0];
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

  // Unrecognized Parameter: teach the codes in place.
  if (badParam) {
    w.addSpacer(2);
    const hint = w.addText("park? use MK · EP · HS · AK");
    hint.font = Font.systemFont(9);
    hint.textColor = new Color("#e0a24b");
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
