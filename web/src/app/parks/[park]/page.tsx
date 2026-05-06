import { notFound } from "next/navigation";
import Link from "next/link";

import { auth } from "@/auth";
import { findPark } from "@/lib/parks";
import { getParkRides } from "@/lib/dynamodb";
import { getUserFavoriteRides } from "@/lib/dynamodb-writes";
import {
  getParkSchedule,
  isExtraHoursNow,
  isParkOpenNow,
} from "@/lib/schedule";
import { RideRow } from "@/components/ride-row";
import { ParkSchedule } from "@/components/park-schedule";

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
  searchParams: Promise<{ favorites?: string }>;
}) {
  const [{ park: parkKeyRaw }, { favorites: favParam }] = await Promise.all([
    params,
    searchParams,
  ]);
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

  const operating = visibleRides.filter((r) => r.status === "OPERATING");
  const down = visibleRides.filter((r) => r.status === "DOWN");
  const closed = visibleRides.filter(
    (r) => r.status === "CLOSED" || r.status === "REFURBISHMENT",
  );

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
            href={`/parks/${park.key}`}
            label="All rides"
            active={!showOnlyFavorites}
          />
          <FilterToggle
            href={`/parks/${park.key}?favorites=1`}
            label={`★ Favorites (${favorites.size})`}
            active={showOnlyFavorites}
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
