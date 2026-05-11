/**
 * Showtimes types + pure helpers — usable from both server and client.
 *
 * The data fetch (`getParkShowtimes`) lives in `showtimes-server.ts`
 * with an `import "server-only"` guard so it can never accidentally
 * ship to the browser. Everything here is safe to import from a
 * client component.
 *
 * See showtimes-server.ts for the M4 design rationale.
 *
 * KEEP IN SYNC: the MCP server (`mcp/server.py`) carries a verbatim
 * Python port of `classifyShow` + `NAMED_ACT_OVERRIDES` so the agentic
 * planner sees the same buckets the web UI does. When a headliner
 * retires or a new act launches and you update the regex / overrides
 * here, also update the matching block in `mcp/server.py` (search for
 * "showtimes (M4 over MCP)"). Drift is non-fatal — MCP would just
 * misclassify show X for one cycle — but keep it tight.
 */

/**
 * Bucket a SHOW gets sorted into for the UI.
 *
 *   spectacular    — fireworks, projection shows, nighttime finales
 *   parade         — daytime parades and cavalcades
 *   stage          — scheduled stage productions you'd plan around
 *   music          — bands, philharmonics, drummers, a cappella groups
 *   atmosphere     — non-musical ambient acts (jugglers, stilt walkers)
 *   character_meet — "Meet [Character]" character meet-and-greets
 *
 * Headliners (UI default-visible) = spectacular | parade | stage.
 * The category-filter pills give one-click access to any single
 * bucket regardless of headliner / more split.
 */
export type ShowCategory =
  | "spectacular"
  | "parade"
  | "stage"
  | "music"
  | "atmosphere"
  | "character_meet";

export const HEADLINER_CATEGORIES: readonly ShowCategory[] = [
  "spectacular",
  "parade",
  "stage",
];

/** Display order for category-filter pills in the UI. */
export const ALL_CATEGORIES: readonly ShowCategory[] = [
  "spectacular",
  "parade",
  "stage",
  "music",
  "character_meet",
  "atmosphere",
];

export interface Showtime {
  /** ISO datetime with TZ offset, e.g. "2026-05-06T15:00:00-04:00" */
  start: string;
  end: string;
}

export interface ShowEntity {
  id: string;
  name: string;
  category: ShowCategory;
  /** Today's performances, sorted ascending by start time. */
  showtimes: Showtime[];
}

export interface ParkShowtimes {
  /** Default-visible: spectaculars, parades, marquee stage shows. */
  headliners: ShowEntity[];
  /** Default-collapsed: atmosphere acts and character meets. */
  more: ShowEntity[];
  /** Soonest unstarted performance across both buckets, or null. */
  nextUp: { show: ShowEntity; time: Showtime } | null;
}

/**
 * Hardcoded overrides for named acts whose API titles don't betray
 * their category — applied before the keyword regex below. These
 * are stable park-specific titles (each WDW headliner runs 1-5
 * years) so the maintenance cost is low; expect to revisit this
 * list when a show retires. Substring match on lowercase name.
 */
const NAMED_ACT_OVERRIDES: { pattern: RegExp; category: ShowCategory }[] = [
  // Stage shows whose names lack the "live on stage" / "musical"
  // keywords that the regex below relies on.
  { pattern: /mickey's magical friendship faire/, category: "stage" },
  { pattern: /celebración encanto|celebracion encanto/, category: "stage" },
  { pattern: /feathered friends in flight/, category: "stage" },
  // The "Spectacular!" in the title trips the spectacular regex
  // before the stage regex's "epic stunt" can match — but Indy is
  // a midday stunt show running ~5x/day, not a nighttime finale.
  { pattern: /indiana jones.*epic stunt/, category: "stage" },
  // Christmas-season stage show at EPCOT (Festival of the Holidays).
  { pattern: /candlelight processional/, category: "stage" },
  // Live-music sets at World Showcase / AK pavilions where the
  // API name describes the venue ("Entertainment at <Stage>") not
  // the act. Locals know these are bands; visitors need the hint.
  { pattern: /viva mexico/, category: "music" },
  { pattern: /entertainment at canada mill stage/, category: "music" },
  { pattern: /entertainment at germany gazebo/, category: "music" },
  // EPCOT festival concert series. Garden Rocks (Flower & Garden,
  // Mar-Jul) and Eat to the Beat (Food & Wine, Aug-Nov) usually
  // contain "Concert" in the API name (caught by the music regex
  // anyway), but these overrides defend against years where the
  // branding drops it. Disney on Broadway (also F&W) sometimes
  // ships without any music keyword and actually needs the override.
  { pattern: /eat to the beat/, category: "music" },
  { pattern: /garden rocks/, category: "music" },
  { pattern: /disney on broadway/, category: "music" },
  // Adventures with Kevin = the bird character from Up; functionally
  // a character moment, not an atmosphere band.
  { pattern: /adventures with kevin/, category: "character_meet" },
];

/**
 * Heuristic name-based classifier. The themeparks.wiki API tags
 * everything as `entityType: "SHOW"` whether it's the 12-min castle
 * fireworks finale or a 30-min character meet, so we can't lean on
 * the API. Patterns below cover the current WDW lineup; anything
 * unmatched falls through to "atmosphere" — wrong but safe (still
 * visible in the collapsed section, never invisible). New shows or
 * renames may need a regex update.
 */
export function classifyShow(name: string): ShowCategory {
  const n = name.toLowerCase();

  // Named-act overrides win over everything else — they exist
  // precisely because the keyword regex misclassifies them.
  for (const o of NAMED_ACT_OVERRIDES) {
    if (o.pattern.test(n)) return o.category;
  }

  // Character meets — every WDW one starts with "Meet ".
  if (/^meet /.test(n)) return "character_meet";

  // Nighttime spectaculars / fireworks. Each park's headliner has
  // turned over multiple times in the last decade — list current
  // titles plus generic markers ("fireworks", "spectacular").
  if (
    /\b(fireworks|spectacular|enchantment|happily ever after|luminous|fantasmic|wonderful world of animation|disney movie magic|tree of life awakenings|disney starlight|symphony of us)\b/.test(n)
  ) {
    return "spectacular";
  }

  // Parades / cavalcades.
  if (/\b(parade|cavalcade)\b/.test(n)) return "parade";

  // Stage shows that guidebooks call out as "must-do."
  if (
    /\b(live on stage|sing-?along|musical adventure|musical celebration|festival of the lion king|finding nemo|epic stunt|frozen sing|first order searches|disney villains|big blue|beauty and the beast)\b/.test(n)
  ) {
    return "stage";
  }

  // Live music — bands, ensembles, choirs, drum corps, individual
  // musicians. Mix of generic terms (band, drum, philharmonic) and
  // named acts that don't otherwise telegraph "music" by keyword
  // (JAMMitors, Matsuriza, Dapper Dans, Eco-Rhythmics). Some named
  // music acts at EPCOT pavilions are listed only as "Entertainment
  // at <Stage>" — those slip through to atmosphere; not worth a
  // hardcoded list of every World Showcase venue.
  if (
    /\b(band|philharmonic|drum|drummers|drummer|pianist|musician|concert|mariachi|marimba|voices of|jammitors|dapper dans|beats and strings|kora tinga|rhythmics|swingin|matsuriza)\b/.test(n)
  ) {
    return "music";
  }

  return "atmosphere";
}

/** First performance of `show` starting after `now`, or null. */
export function nextUpcomingTime(show: ShowEntity, now: Date): Showtime | null {
  for (const t of show.showtimes) {
    if (new Date(t.start) > now) return t;
  }
  return null;
}

export function isShowtimeInPast(t: Showtime, now: Date): boolean {
  return new Date(t.start) <= now;
}

export function formatShowtime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  });
}
