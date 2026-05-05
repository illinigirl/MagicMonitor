import { notFound } from "next/navigation";
import Link from "next/link";

import { findPark } from "@/lib/parks";
import { getParkRides } from "@/lib/dynamodb";
import {
  getParkSchedule,
  isExtraHoursNow,
  isParkOpenNow,
} from "@/lib/schedule";
import { RideRow } from "@/components/ride-row";
import { ParkSchedule } from "@/components/park-schedule";

// Server-render fresh on each request — DynamoDB query is cheap and
// the data changes every 2 min, so caching past ~30s would be stale.
export const revalidate = 30;

export default async function ParkPage({
  params,
}: {
  params: Promise<{ park: string }>;
}) {
  const { park: parkKeyRaw } = await params;
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  // Parallel fetch — DynamoDB scan + themeparks.wiki schedule are
  // independent and both block render.
  const [rides, schedule] = await Promise.all([
    getParkRides(park.key),
    getParkSchedule(park.key),
  ]);

  const parkIsOpen = schedule
    ? isParkOpenNow(schedule) || isExtraHoursNow(schedule)
    : true;

  const operating = rides.filter((r) => r.status === "OPERATING");
  const down = rides.filter((r) => r.status === "DOWN");
  const closed = rides.filter(
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
      </div>

      {/* When the park is closed, render a different empty-state up
          front rather than a misleading "24 rides operating" pulled
          from the last poll before close. The poller keeps writing
          state but the data is stale until tomorrow. */}
      {!parkIsOpen ? (
        <ClosedStateNotice rideCount={rides.length} downCount={down.length} />
      ) : (
        <p className="label-meta mt-4">
          {rides.length} attractions · {operating.length} open ·
          {" "}
          {down.length} down · {closed.length} closed
        </p>
      )}

      {/* DOWN section first — it's why you opened the page */}
      {parkIsOpen && down.length > 0 && (
        <Section title={`Down (${down.length})`} accent="bad">
          <ul>
            {down.map((r) => (
              <RideRow key={r.ride_id} ride={r} />
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
                <RideRow key={r.ride_id} ride={r} />
              ))}
            </ul>
          )}
        </Section>
      )}

      {parkIsOpen && closed.length > 0 && (
        <Section title={`Closed or in refurb (${closed.length})`}>
          <ul>
            {closed.map((r) => (
              <RideRow key={r.ride_id} ride={r} />
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
