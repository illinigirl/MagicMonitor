// Magic Monitor — iOS home-screen TODAY'S PLAN widget (Scriptable).
//
// Sibling of scriptable-waits.js: that one answers "what are the waits?",
// this one answers "what's our day?" — today's plan as one timeline:
// rides in plan order with meals (🍽 booked / 🥪 suggested) and shows (🎭)
// slotted in by time, ✓ on rides already done, 🎟 on held Lightning
// Lanes. The NEXT ride (first not done) is highlighted gold.
//
// Setup (one time):
//   1. Install "Scriptable" from the App Store.
//   2. New script → paste this file → name it "MM Plan".
//   3. Sign into magicmonitor.megillini.dev/trips on your phone, expand
//      "Phone widget setup — today's plan", copy your private feed URL,
//      and paste it into FEED_URL below. Treat that URL like a password
//      (it can read the family's shared plan).
//   4. Long-press home screen → add a Scriptable widget → choose
//      "MM Plan". Medium/large fit the most of the day.
//
// Tapping the widget opens the live schedule (/replan) where you can
// mark rides done, drop them, or ask Claude to re-plan.
//
// iOS refreshes widgets on its own cadence (~5–15 min).

const FEED_URL = "PASTE_YOUR_FEED_URL_HERE";

// How many timeline rows fit, by widget size.
function rowBudget() {
  switch (config.widgetFamily) {
    case "small": return 5;
    case "large": return 16;
    default: return 8; // medium (and the in-app preview)
  }
}

const GOLD = new Color("#d4af37");
const MUTED = new Color("#8a8378");

async function run() {
  const w = new ListWidget();
  w.backgroundColor = new Color("#141210");
  w.url = "https://magicmonitor.megillini.dev/trips";
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

  // Header: park (or brand) left, weather right — same as the waits widget.
  const head = w.addStack();
  head.centerAlignContent();
  const title = head.addText(data.found ? data.park_name : "Magic Monitor");
  title.font = Font.boldSystemFont(13);
  title.textColor = GOLD;
  title.lineLimit = 1;
  head.addSpacer();
  if (data.weather) {
    const wx = head.addText(`${data.weather.icon} ${data.weather.temp_f}°`);
    wx.font = Font.systemFont(12);
    wx.textColor = Color.white();
  }

  if (!data.found) {
    w.addSpacer(6);
    const none = w.addText("No plan today — tap to see your trips.");
    none.font = Font.systemFont(11);
    none.textColor = Color.gray();
    return finish(w);
  }

  // Deep-link straight into today's live schedule.
  w.url = `https://magicmonitor.megillini.dev/replan?plan=${encodeURIComponent(data.plan_id)}`;

  const sub = w.addText(data.active ? "Today's plan · live" : "Today's plan · not activated");
  sub.font = Font.mediumSystemFont(9);
  sub.textColor = MUTED;
  w.addSpacer(6);

  const entries = data.entries ?? [];
  // Lead with what's NEXT: skip done rides once the list won't all fit,
  // but keep one ✓ line of context so progress is visible.
  const budget = rowBudget();
  let rows = entries;
  if (entries.length > budget) {
    const firstPending = entries.findIndex((e) => e.kind !== "ride" || !e.done);
    const start = Math.max(0, firstPending - 1);
    rows = entries.slice(start);
  }

  let highlighted = false;
  for (const e of rows.slice(0, budget)) {
    const line = w.addStack();
    line.centerAlignContent();

    if (e.kind === "ride") {
      const isNext = !e.done && !highlighted;
      if (isNext) highlighted = true;
      const name = line.addText(`${e.done ? "✓ " : ""}${e.name}`);
      name.font = isNext ? Font.boldSystemFont(11) : Font.systemFont(11);
      name.textColor = e.done ? MUTED : isNext ? GOLD : Color.white();
      name.lineLimit = 1;
      line.addSpacer();
      const right = e.held_ll ? `🎟 ${e.held_ll}` : e.time ?? "";
      if (right) {
        const val = line.addText(right);
        val.font = Font.boldMonospacedSystemFont(10);
        val.textColor = e.done ? MUTED : Color.white();
      }
    } else {
      const icon = e.kind === "show" ? "🎭" : e.booked ? "🍽" : "🥪";
      const name = line.addText(`${icon} ${e.name}`);
      name.font = Font.systemFont(11);
      name.textColor = MUTED;
      name.lineLimit = 1;
      line.addSpacer();
      const val = line.addText(e.time ?? "");
      val.font = Font.boldMonospacedSystemFont(10);
      val.textColor = MUTED;
    }
    w.addSpacer(3);
  }

  // Show when we've trimmed, so a hidden stop isn't a silent surprise.
  const shown = Math.min(rows.length, budget);
  if (entries.length > shown) {
    w.addSpacer(2);
    const more = w.addText(`+${entries.length - shown} more — tap for the day`);
    more.font = Font.systemFont(9);
    more.textColor = MUTED;
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
