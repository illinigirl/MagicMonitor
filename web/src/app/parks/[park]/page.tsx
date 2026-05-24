import { notFound } from "next/navigation";
import Link from "next/link";

import { auth } from "@/auth";
import { findPark } from "@/lib/parks";
import { getParkRides, type RideState } from "@/lib/dynamodb";
import { getUserFavoriteRides } from "@/lib/dynamodb-writes";
import {
  getParkSchedule,
  isExtraHoursNow,
  isParkOpenNow,
} from "@/lib/schedule";
import { RideRow } from "@/components/ride-row";
import { ParkSchedule } from "@/components/park-schedule";
import { UpdatedIndicator } from "@/components/updated-indicator";

// Sort modes the user can pick from. wait_asc is the default because
// "what's the shortest line right now" is the most actionable framing
// when standing in a park. Favorites pin to the top regardless of
// sort mode, so a signed-in user always sees their rides first.
type SortMode = "wait_asc" | "wait_desc" | "alpha";
const DEFAULT_SORT: SortMode = "wait_asc";

function parseSort(raw: string | undefined): SortMode {
  if (raw === "wait_desc" || raw === "alpha" || raw === "wait_asc") return raw;
  return DEFAULT_SORT;
}

/**
 * Apply favorites-first ordering + the user's chosen sort within each
 * favorites bucket. Stable: alphabetical secondary sort already came
 * from getParkRides, so equal-wait rides land in a predictable order.
 *
 * Note on wait_mins null: not-OPERATING rides (DOWN / CLOSED /
 * REFURBISHMENT) carry null wait, and we want them at the bottom of
 * the sort either direction — being "shortest" because wait is
 * unknown would mislead. Using positive infinity for null pushes
 * them to the bottom of ascending sort; for descending sort we use
 * negative infinity so they don't crowd the top.
 */
function sortRides(
  rides: RideState[],
  mode: SortMode,
  favorites: Set<string>,
): RideState[] {
  const sortFn = (a: RideState, b: RideState) => {
    if (mode === "alpha") return a.name.localeCompare(b.name);
    const aw =
      a.wait_mins ?? (mode === "wait_asc" ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY);
    const bw =
      b.wait_mins ?? (mode === "wait_asc" ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY);
    return mode === "wait_asc" ? aw - bw : bw - aw;
  };
  const favs = rides.filter((r) => favorites.has(r.ride_id));
  const others = rides.filter((r) => !favorites.has(r.ride_id));
  favs.sort(sortFn);
  others.sort(sortFn);
  return [...favs, ...others];
}

/**
 * URL builder that preserves the current set of view params while
 * overriding one. Lets the pill rows construct correct hrefs without
 * forgetting to carry forward the other filter/sort.
 */
function parkHref(
  parkKey: string,
  current: { favorites: boolean; sort: SortMode },
  override: Partial<{ favorites: boolean; sort: SortMode }>,
): string {
  const merged = { ...current, ...override };
  const params = new URLSearchParams();
  if (merged.favorites) params.set("favorites", "1");
  if (merged.sort !== DEFAULT_SORT) params.set("sort", merged.sort);
  const qs = params.toString();
  return `/parks/${parkKey}${qs ? `?${qs}` : ""}`;
}

// Server-render fresh on each request — DynamoDB query is cheap and
// the data changes every 2 min, so caching past ~30s would be stale.
// Note: must stay <=30 because the favorites filter (?favorites=1)
// is read from searchParams and we want toggle changes visible
// immediately, not after a cache expiry.
export const revalidate = 30;

export default async function ParkPage({
  params,
  searchParams,
}: {
  params: Promise<{ park: string }>;
  searchParams: Promise<{ favorites?: string; sort?: string }>;
}) {
  const [{ park: parkKeyRaw }, { favorites: favParam, sort: sortParam }] =
    await Promise.all([params, searchParams]);
  const sortMode = parseSort(sortParam);
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  // Parallel fetch — DDB ride scan, themeparks.wiki schedule, and
  // (when signed in) the user's favorites all block render. Run
  // together rather than sequentially.
  const session = await auth();
  const sub = session?.user?.id;
  const [rides, schedule, favorites] = await Promise.all([
    getParkRides(park.key),
    getParkSchedule(park.key),
    sub ? getUserFavoriteRides(sub, park.key) : Promise.resolve(new Set<string>()),
  ]);

  const parkIsOpen = schedule
    ? isParkOpenNow(schedule) || isExtraHoursNow(schedule)
    : true;

  // ?favorites=1 narrows the list to just favorited rides. Off by
  // default (anonymous + signed-in alike see everything) so the
  // page still serves as a public ride-status snapshot.
  const showOnlyFavorites = favParam === "1" && favorites.size > 0;
  const visibleRides = showOnlyFavorites
    ? rides.filter((r) => favorites.has(r.ride_id))
    : rides;

  // Apply favorites-first + chosen sort per status group. Operating
  // is where the sort matters most (wait time is meaningful); DOWN
  // and CLOSED apply the same logic but fall back to favorites-first
  // + alpha-secondary since wait is null for those rides.
  const operating = sortRides(
    visibleRides.filter((r) => r.status === "OPERATING"),
    sortMode,
    favorites,
  );
  const down = sortRides(
    visibleRides.filter((r) => r.status === "DOWN"),
    sortMode,
    favorites,
  );
  const closed = sortRides(
    visibleRides.filter(
      (r) => r.status === "CLOSED" || r.status === "REFURBISHMENT",
    ),
    sortMode,
    favorites,
  );

  // Freshness signal for the "Updated Xs ago" indicator. Use the
  // newest last_seen across all rides at the park — that's the most
  // recent successful poll. ISO strings sort lexicographically so
  // .sort().pop() is equivalent to max() without a Date round-trip.
  const lastSeenIso =
    rides
      .map((r) => r.last_seen)
      .filter((s): s is string => Boolean(s))
      .sort()
      .pop() ?? null;

  // Server-rendered initial value for the indicator. Absolute clock
  // time in ET so the user sees something meaningful even with JS
  // disabled and so the SSR/CSR hydration matches deterministically
  // (locale + tz pinned explicitly).
  const initialAbsolute = lastSeenIso
    ? new Date(lastSeenIso).toLocaleTimeString("en-US", {
        timeZone: "America/New_York",
        hour: "numeric",
        minute: "2-digit",
      })
    : null;

  return (
    <div
      className="mx-auto max-w-4xl px-6 py-10"
      style={
        { "--park-accent": `var(${park.accentVar})` } as React.CSSProperties
      }
    >
      {/* Park header — title + accent strip + back link */}
      <div className="mb-8">
        <Link
          href="/"
          className="text-fg-3 hover:text-fg-1 text-sm transition-colors"
        >
          ← All parks
        </Link>
        <div className="flex items-baseline gap-3 mt-3">
          <h2 className="display text-4xl font-medium">{park.name}</h2>
          <span
            className="h-1 w-12 rounded-full"
            style={{ background: "var(--park-accent)" }}
          />
        </div>
        <p className="text-fg-2 mt-2">{park.tagline}</p>
        <ParkSchedule schedule={schedule} />
        {/* Showtimes live behind a click — most visits are ride-driven
            and a flat list of 60-90 daily entertainment slots would
            crowd the page. Subtle text link keeps it discoverable
            without competing with the down-rides above. */}
        <Link
          href={`/parks/${park.key}/today`}
          className="inline-block mt-3 text-sm transition-colors hover:opacity-80"
          style={{ color: "var(--gold)" }}
        >
          Today&apos;s shows →
        </Link>
      </div>

      {/* Filter toggle — only shown when signed in AND user has at
          least one favorite in this park. Hides itself otherwise to
          avoid a dead control on first visits. */}
      {sub && favorites.size > 0 && (
        <div className="mt-4 flex items-center gap-3 text-sm">
          <span className="label-meta">View:</span>
          <FilterToggle
            href={parkHref(park.key, { favorites: showOnlyFavorites, sort: sortMode }, { favorites: false })}
            label="All rides"
            active={!showOnlyFavorites}
          />
          <FilterToggle
            href={parkHref(park.key, { favorites: showOnlyFavorites, sort: sortMode }, { favorites: true })}
            label={`★ Favorites (${favorites.size})`}
            active={showOnlyFavorites}
          />
        </div>
      )}

      {/* Sort toggles — always visible when the park is open so the
          user has agency over the ride list ordering. Favorites pin
          to the top within each sort, so the choice is "what order
          for everything else." Default is wait_asc ("what's the
          shortest line right now"). */}
      {parkIsOpen && (
        <div className="mt-3 flex items-center gap-3 text-sm">
          <span className="label-meta">Sort:</span>
          <FilterToggle
            href={parkHref(park.key, { favorites: showOnlyFavorites, sort: sortMode }, { sort: "wait_asc" })}
            label="Wait ↑"
            active={sortMode === "wait_asc"}
          />
          <FilterToggle
            href={parkHref(park.key, { favorites: showOnlyFavorites, sort: sortMode }, { sort: "wait_desc" })}
            label="Wait ↓"
            active={sortMode === "wait_desc"}
          />
          <FilterToggle
            href={parkHref(park.key, { favorites: showOnlyFavorites, sort: sortMode }, { sort: "alpha" })}
            label="A–Z"
            active={sortMode === "alpha"}
          />
        </div>
      )}

      {/* When the park is closed, render a different empty-state up
          front rather than a misleading "24 rides operating" pulled
          from the last poll before close. The poller keeps writing
          state but the data is stale until tomorrow. */}
      {!parkIsOpen ? (
        <ClosedStateNotice rideCount={rides.length} downCount={down.length} />
      ) : (
        <p className="label-meta mt-4">
          {visibleRides.length} attractions · {operating.length} open ·
          {" "}
          {down.length} down · {closed.length} closed
          {showOnlyFavorites && " (favorites only)"}
          {lastSeenIso && initialAbsolute && (
            <>
              {" · "}
              <UpdatedIndicator
                iso={lastSeenIso}
                initialAbsolute={initialAbsolute}
              />
            </>
          )}
        </p>
      )}

      {/* DOWN section first — it's why you opened the page */}
      {parkIsOpen && down.length > 0 && (
        <Section title={`Down (${down.length})`} accent="bad">
          <ul>
            {down.map((r) => (
              <RideRow key={r.ride_id} ride={r} isFavorite={favorites.has(r.ride_id)} />
            ))}
          </ul>
        </Section>
      )}

      {parkIsOpen && (
        <Section title={`Open (${operating.length})`}>
          {operating.length === 0 ? (
            <p className="text-fg-2 py-4">
              Nothing operating right now.
            </p>
          ) : (
            <ul>
              {operating.map((r) => (
                <RideRow key={r.ride_id} ride={r} isFavorite={favorites.has(r.ride_id)} />
              ))}
            </ul>
          )}
        </Section>
      )}

      {parkIsOpen && closed.length > 0 && (
        <Section title={`Closed or in refurb (${closed.length})`}>
          <ul>
            {closed.map((r) => (
              <RideRow key={r.ride_id} ride={r} isFavorite={favorites.has(r.ride_id)} />
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function ClosedStateNotice({
  rideCount,
  downCount,
}: {
  rideCount: number;
  downCount: number;
}) {
  return (
    <div className="mt-8 rounded-lg border border-line bg-bg-1 px-5 py-6 shadow-[var(--shadow-card)]">
      <p className="display text-lg font-medium text-fg-1">
        The park is currently closed.
      </p>
      <p className="text-fg-2 text-sm mt-2 leading-relaxed">
        We&apos;re still polling — last data shows {rideCount} attractions
        tracked
        {downCount > 0
          ? `, with ${downCount} down at the time of close`
          : ""}.
        Live ride status returns when the park reopens.
      </p>
    </div>
  );
}

/** Pill-style link that highlights when the current view matches.
 * Plain anchor tag so server-rendering picks up the correct active
 * state without client JS. */
function FilterToggle({
  href,
  label,
  active,
}: {
  href: string;
  label: string;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
        active
          ? "bg-gold text-gold-ink"
          : "bg-bg-1 text-fg-2 hover:bg-bg-2"
      }`}
      aria-current={active ? "page" : undefined}
    >
      {label}
    </Link>
  );
}

function Section({
  title,
  accent,
  children,
}: {
  title: string;
  accent?: "bad";
  children: React.ReactNode;
}) {
  return (
    <section className="mt-10 first:mt-0">
      <h3
        className={`display text-xl font-medium mb-3 ${accent === "bad" ? "text-bad" : "text-fg-1"}`}
      >
        {title}
      </h3>
      <div className="rounded-lg border border-line bg-bg-1 px-4 py-2 shadow-[var(--shadow-card)]">
        {children}
      </div>
    </section>
  );
}
