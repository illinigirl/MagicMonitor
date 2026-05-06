"use client";

import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";

import {
  ALL_CATEGORIES,
  formatShowtime,
  isShowtimeInPast,
  type ParkShowtimes,
  type ShowCategory,
  type ShowEntity,
  type Showtime,
} from "@/lib/showtimes";

/**
 * Client-side renderer for the M4 "Today at the park" page.
 *
 * Server fetches once and hands us bucketed data; we own:
 *   1. Search input (case-insensitive substring on show name).
 *   2. Category filter pills — one-click narrow to spectacular /
 *      parade / stage / music / character / atmosphere. When a
 *      category is active, the headliners/more split collapses into
 *      one flat list.
 *   3. Default-collapsed "More entertainment" — atmosphere + music +
 *      character meets bloat the list (12-90+ rows per park) and
 *      most visitors don't plan around them. Headliners stay open.
 *   4. Past-performance greying — a band set at 11 AM is still
 *      relevant ("missed it") but visually de-emphasized.
 *   5. "Next up" callout — single most useful summary on the page,
 *      hidden during search or active filter.
 */
export function TodayAtThePark({ showtimes }: { showtimes: ParkShowtimes }) {
  const [query, setQuery] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState<ShowCategory | null>(null);

  const q = query.trim().toLowerCase();
  const isSearching = q.length > 0;
  const isFiltering = categoryFilter !== null;

  // Ticks once a minute on the client so the "in N min" countdown
  // and the past-show greying update without waiting for the page
  // to revalidate. Server-side render starts with a fresh `new Date()`,
  // then the effect takes over hydration-side. Cheap (one setState/min)
  // and avoids a stale-clock UX where a 5-min countdown lingers at "5"
  // for the full 60s revalidate window.
  const [now, setNow] = useState<Date>(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  // Per-category counts for the pill row. Computed once from the
  // server-bucketed data; doesn't depend on search/filter state.
  const categoryCounts = useMemo(() => {
    const counts = new Map<ShowCategory, number>();
    for (const s of [...showtimes.headliners, ...showtimes.more]) {
      counts.set(s.category, (counts.get(s.category) ?? 0) + 1);
    }
    return counts;
  }, [showtimes]);
  const totalCount = showtimes.headliners.length + showtimes.more.length;

  // When a category filter is active, flatten both buckets into one
  // category-narrowed list. Otherwise keep the headliners/more split.
  const filteredHeadliners = useMemo(
    () => filterShows(showtimes.headliners, q, categoryFilter),
    [showtimes.headliners, q, categoryFilter],
  );
  const filteredMore = useMemo(
    () => filterShows(showtimes.more, q, categoryFilter),
    [showtimes.more, q, categoryFilter],
  );
  const filteredFlat = useMemo(
    () => [...filteredHeadliners, ...filteredMore],
    [filteredHeadliners, filteredMore],
  );

  const totalMatches = filteredFlat.length;
  const showMoreSection = isSearching || moreOpen;

  return (
    <div className="space-y-6">
      <SearchBar value={query} onChange={setQuery} />

      <CategoryPills
        active={categoryFilter}
        counts={categoryCounts}
        totalCount={totalCount}
        onSelect={setCategoryFilter}
      />

      {(isSearching || isFiltering) && totalMatches === 0 && (
        <p className="text-fg-2 text-sm">
          {isSearching
            ? <>No shows match &ldquo;{query}&rdquo;{isFiltering && " in this category"}.</>
            : <>No shows in this category today.</>}
        </p>
      )}

      {!isSearching && !isFiltering && showtimes.nextUp && (
        <NextUpCallout
          show={showtimes.nextUp.show}
          time={showtimes.nextUp.time}
          now={now}
        />
      )}

      {/* When a category filter is active, render one flat section
          named after the category (e.g., "Music"). When not filtering,
          fall back to the original headliners + collapsible-more split. */}
      {isFiltering ? (
        filteredFlat.length > 0 && (
          <section>
            <h3 className="display text-xl font-medium mb-3 text-fg-1">
              {labelForCategory(categoryFilter!)}
              <span className="label-meta ml-2">({filteredFlat.length})</span>
            </h3>
            <ShowList shows={filteredFlat} now={now} />
          </section>
        )
      ) : (
        <>
          {filteredHeadliners.length > 0 && (
            <section>
              <h3 className="display text-xl font-medium mb-3 text-fg-1">
                Headliners
              </h3>
              <ShowList shows={filteredHeadliners} now={now} />
            </section>
          )}

          {(isSearching ? filteredMore.length > 0 : showtimes.more.length > 0) && (
            <section>
              {isSearching ? (
                <h3 className="display text-xl font-medium mb-3 text-fg-1">
                  More entertainment{" "}
                  <span className="label-meta">({filteredMore.length})</span>
                </h3>
              ) : (
                <button
                  type="button"
                  onClick={() => setMoreOpen((v) => !v)}
                  aria-expanded={moreOpen}
                  className="display text-xl font-medium mb-3 text-fg-1 flex items-center gap-2 hover:text-fg-0 transition-colors"
                >
                  <span>More entertainment</span>
                  <span className="label-meta">({showtimes.more.length})</span>
                  <span
                    className={clsx(
                      "text-fg-3 text-base transition-transform",
                      moreOpen && "rotate-90",
                    )}
                    aria-hidden="true"
                  >
                    ›
                  </span>
                </button>
              )}
              {showMoreSection && filteredMore.length > 0 && (
                <ShowList shows={filteredMore} now={now} />
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}

function filterShows(
  shows: ShowEntity[],
  q: string,
  category: ShowCategory | null,
): ShowEntity[] {
  let out = shows;
  if (category) out = out.filter((s) => s.category === category);
  if (q) out = out.filter((s) => s.name.toLowerCase().includes(q));
  return out;
}

function SearchBar({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <input
      type="search"
      placeholder="Search shows…"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      autoComplete="off"
      className="w-full rounded-md border border-line bg-bg-1 px-3 py-2 text-sm text-fg-1 placeholder:text-fg-3 focus:outline-none focus:border-gold focus:ring-1 focus:ring-gold"
      aria-label="Search shows"
    />
  );
}

function CategoryPills({
  active,
  counts,
  totalCount,
  onSelect,
}: {
  active: ShowCategory | null;
  counts: Map<ShowCategory, number>;
  totalCount: number;
  onSelect: (c: ShowCategory | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      <Pill
        label="All"
        count={totalCount}
        active={active === null}
        onClick={() => onSelect(null)}
      />
      {ALL_CATEGORIES.map((cat) => {
        const count = counts.get(cat) ?? 0;
        // Hide buckets with no shows today — no value in showing
        // "Parade (0)" at parks where there isn't one running.
        if (count === 0) return null;
        return (
          <Pill
            key={cat}
            label={labelForCategory(cat)}
            count={count}
            active={active === cat}
            onClick={() => onSelect(active === cat ? null : cat)}
          />
        );
      })}
    </div>
  );
}

function Pill({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        "rounded-full px-3 py-1 text-xs font-medium transition-colors",
        active
          ? "bg-gold text-gold-ink"
          : "bg-bg-1 text-fg-2 hover:bg-bg-2",
      )}
      style={active ? { color: "var(--gold-ink)" } : undefined}
    >
      {label} <span className="opacity-70">({count})</span>
    </button>
  );
}

function NextUpCallout({
  show,
  time,
  now,
}: {
  show: ShowEntity;
  time: Showtime;
  now: Date;
}) {
  // Read from the ticking `now` (parent state) so the countdown
  // updates each minute on the client. Using Date.now() here would
  // freeze it at first paint until the next page revalidate.
  const minsUntil = Math.round(
    (new Date(time.start).getTime() - now.getTime()) / 60000,
  );
  return (
    <div
      className="rounded-lg border border-line px-5 py-4 shadow-[var(--shadow-card)]"
      style={{ background: "var(--gold-soft)" }}
    >
      <div className="label-meta" style={{ color: "var(--gold)" }}>
        Next up
      </div>
      <div className="display text-lg font-medium text-fg-0 mt-1">
        {show.name}
      </div>
      <div className="text-fg-1 text-sm mt-1 flex items-baseline gap-2 flex-wrap">
        <span className="tabular-nums">{formatShowtime(time.start)}</span>
        {/* Only show the countdown when it's reasonably soon — "in
            420 min" is just noise; a negative would mean the time
            already passed (cap defensively). */}
        {minsUntil > 0 && minsUntil <= 90 && (
          <span className="label-meta">in {minsUntil} min</span>
        )}
        <CategoryBadge category={show.category} />
      </div>
    </div>
  );
}

function ShowList({ shows, now }: { shows: ShowEntity[]; now: Date }) {
  return (
    <ul className="rounded-lg border border-line bg-bg-1 px-4 py-2 shadow-[var(--shadow-card)]">
      {shows.map((s) => (
        <ShowRow key={s.id} show={s} now={now} />
      ))}
    </ul>
  );
}

/**
 * One show row — name + category badge on top, time chips on a
 * second line. Stacked (not grid) because some atmosphere acts have
 * 15+ performances per day and a side-by-side `[1fr_auto]` grid
 * collapses the name into a vertical character strip when the chip
 * column eats all the available width.
 */
function ShowRow({ show, now }: { show: ShowEntity; now: Date }) {
  return (
    <li className="border-b border-line-soft py-3 last:border-b-0">
      <div className="flex items-baseline gap-2 flex-wrap mb-2">
        <span className="text-fg-0 font-medium">{show.name}</span>
        <CategoryBadge category={show.category} />
      </div>
      <div className="flex flex-wrap gap-1.5">
        {show.showtimes.map((t, i) => (
          <ShowtimeChip key={i} time={t} now={now} />
        ))}
      </div>
    </li>
  );
}

function ShowtimeChip({ time, now }: { time: Showtime; now: Date }) {
  const past = isShowtimeInPast(time, now);
  return (
    <span
      className={clsx(
        "rounded-sm px-2 py-0.5 text-xs tabular-nums",
        past
          ? "bg-bg-2 text-fg-3 line-through decoration-fg-3/60"
          : "bg-bg-2 text-fg-1",
      )}
      title={past ? "Already started" : undefined}
    >
      {formatShowtime(time.start)}
    </span>
  );
}

function CategoryBadge({ category }: { category: ShowCategory }) {
  const { label, classes, style } = badgeFor(category);
  return (
    <span
      className={clsx(
        "rounded-sm px-1.5 py-0.5 text-[10px] uppercase tracking-wider font-semibold",
        classes,
      )}
      style={style}
    >
      {label}
    </span>
  );
}

function labelForCategory(category: ShowCategory): string {
  return badgeFor(category).label;
}

function badgeFor(category: ShowCategory): {
  label: string;
  classes: string;
  style?: React.CSSProperties;
} {
  switch (category) {
    case "spectacular":
      // Inline styles mirror the gold/gold-soft pattern used in
      // park-schedule.tsx — Tailwind's arbitrary-color compile of
      // these OKLCH vars is finicky and the inline form is what
      // already works elsewhere in the project.
      return {
        label: "Spectacular",
        classes: "",
        style: { background: "var(--gold-soft)", color: "var(--gold)" },
      };
    case "parade":
      return { label: "Parade", classes: "bg-info/15 text-info" };
    case "stage":
      return { label: "Stage", classes: "bg-ok/15 text-ok" };
    case "music":
      return {
        label: "Music",
        classes: "",
        style: { background: "var(--pink-soft)", color: "var(--pink)" },
      };
    case "atmosphere":
      return { label: "Atmosphere", classes: "bg-bg-2 text-fg-2" };
    case "character_meet":
      return { label: "Character", classes: "bg-bg-2 text-fg-2" };
  }
}
