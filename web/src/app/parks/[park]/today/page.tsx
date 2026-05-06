import { notFound } from "next/navigation";
import Link from "next/link";

import { findPark } from "@/lib/parks";
import { getParkShowtimes } from "@/lib/showtimes-server";
import { TodayAtThePark } from "@/components/today-at-the-park";

// 60s page revalidate — the underlying themeparks.wiki fetch is
// cached for 10 min in the lib, so this just controls how often the
// "Next up" callout re-evaluates against the wall clock. Cheap.
export const revalidate = 60;

export default async function TodayPage({
  params,
}: {
  params: Promise<{ park: string }>;
}) {
  const { park: parkKeyRaw } = await params;
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  const showtimes = await getParkShowtimes(park.key);
  const hasAnyShows =
    !!showtimes &&
    (showtimes.headliners.length > 0 || showtimes.more.length > 0);

  return (
    <div
      className="mx-auto max-w-4xl px-6 py-10"
      style={
        { "--park-accent": `var(${park.accentVar})` } as React.CSSProperties
      }
    >
      <div className="mb-8">
        <Link
          href={`/parks/${park.key}`}
          className="text-fg-3 hover:text-fg-1 text-sm transition-colors"
        >
          ← {park.name} rides
        </Link>
        <div className="flex items-baseline gap-3 mt-3 flex-wrap">
          <h2 className="display text-4xl font-medium">
            Today at {park.shortName}
          </h2>
          <span
            className="h-1 w-12 rounded-full"
            style={{ background: "var(--park-accent)" }}
          />
        </div>
        <p className="text-fg-2 mt-2">
          Parades, fireworks, stage shows, and atmosphere acts —
          today&apos;s entertainment lineup at {park.name}.
        </p>
      </div>

      {hasAnyShows ? (
        <TodayAtThePark showtimes={showtimes!} />
      ) : (
        <p className="text-fg-2 mt-8 leading-relaxed">
          {showtimes
            ? "No scheduled entertainment today."
            : "Showtime data is unavailable right now. Try again in a few minutes."}
        </p>
      )}
    </div>
  );
}
