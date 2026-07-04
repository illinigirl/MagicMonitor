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

/**
 * Redemption-window label from a Lightning Lane return start:
 * "2026-07-04T13:15:00-04:00" → "1:15–2:15 PM" (MLL windows run one
 * hour). The start's meridiem is shown only when it differs from the
 * end's ("11:30 AM–12:30 PM").
 */
export function formatEtWindow(iso: string, durationMin = 60): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return "";
  const startMins = Number(m[1]) * 60 + Number(m[2]);
  const endMins = (startMins + durationMin) % (24 * 60);
  const label = (mins: number, withMeridiem: boolean) => {
    const h24 = Math.floor(mins / 60);
    const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
    const mm = String(mins % 60).padStart(2, "0");
    return `${h12}:${mm}${withMeridiem ? ` ${h24 < 12 ? "AM" : "PM"}` : ""}`;
  };
  const sameMeridiem = startMins < 720 === endMins < 720;
  return `${label(startMins, !sameMeridiem)}–${label(endMins, true)}`;
}

/**
 * "3:15 PM" / "3pm" / "15:15" / full ISO → ET ISO on `dateIso` — the TS
 * mirror of the MCP's parse_ll_time (mcp/_tool_impls.py, keep the
 * accepted forms in sync). Returns null when unparseable. The offset is
 * fixed per date via the ET rule (WDW never leaves Eastern).
 */
export function parseEtTime(raw: string, dateIso: string): string | null {
  const s = (raw ?? "").trim();
  if (!s) return null;
  // Full ISO with an offset → trust it as-is.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) && /[+-]\d{2}:\d{2}|Z$/.test(s)) {
    return Number.isFinite(Date.parse(s)) ? s : null;
  }
  let m = /^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$/.exec(s);
  let hh: number, mm: number, ap: string;
  if (m) {
    [hh, mm, ap] = [Number(m[1]), Number(m[2]), (m[3] ?? "").toLowerCase()];
  } else if ((m = /^(\d{1,2})\s*([AaPp][Mm])$/.exec(s))) {
    [hh, mm, ap] = [Number(m[1]), 0, m[2].toLowerCase()];
  } else {
    return null;
  }
  if (ap === "pm" && hh !== 12) hh += 12;
  else if (ap === "am" && hh === 12) hh = 0;
  if (hh > 23 || mm > 59) return null;
  // ET offset for the date: EDT (-04:00) Mar–Oct is right for park days;
  // derive properly from a probe date to cover the edges.
  const probe = new Date(`${dateIso}T12:00:00Z`);
  const tz = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    timeZoneName: "longOffset",
  })
    .formatToParts(probe)
    .find((p) => p.type === "timeZoneName")?.value;
  const off = (tz ?? "GMT-04:00").replace("GMT", "") || "-04:00";
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${dateIso}T${p2(hh)}:${p2(mm)}:00${off}`;
}
