import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ParkCard } from "@/components/park-card";
import { getUserProfile } from "@/lib/dynamodb-writes";
import { PARKS } from "@/lib/parks";
import { getParkSchedule } from "@/lib/schedule";

// Force dynamic so the auth-state check below runs on each request
// rather than getting cached for 10 min. Schedule fetches inside are
// still cached individually via getParkSchedule's own caching.
export const dynamic = "force-dynamic";

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ welcome?: string }>;
}) {
  const sp = await searchParams;
  const session = await auth();

  // Phase 3 onboarding gate: a signed-in user with no PROFILE row
  // has never been to /me. Bounce them there so they don't browse
  // park pages thinking they'll get alerts when they actually
  // won't (no profile = no Pushover key = nothing to alert).
  // The ?welcome=1 query param tells /me to show the first-run
  // copy of the setup banner. Bypass with ?skip=1 for testing.
  if (session?.user?.id && sp.welcome !== "skipped") {
    const profile = await getUserProfile(session.user.id);
    if (!profile) {
      redirect("/me?welcome=1");
    }
  }

  // Fetch all 4 schedules in parallel so the cards render hours+status
  // without four sequential round trips.
  const schedules = await Promise.all(PARKS.map((p) => getParkSchedule(p.key)));

  return (
    <div className="mx-auto max-w-6xl px-6 py-12">
      <section className="max-w-2xl">
        <p className="label-meta">Live status</p>
        <h2 className="display text-4xl font-medium mt-2">
          Pick a park.
        </h2>
        <p className="text-fg-2 mt-3 leading-relaxed">
          Wait times and ride status update every two minutes. Down rides
          surface to the top so you don&apos;t have to scan past the
          carousel of merchandise to find them.
        </p>
        <p className="mt-4 text-sm">
          <Link
            href="/analytics"
            className="transition-opacity hover:opacity-80"
            style={{ color: "var(--gold)" }}
          >
            See historical wait-time analytics →
          </Link>
        </p>
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-10">
        {PARKS.map((park, i) => (
          <ParkCard key={park.key} park={park} schedule={schedules[i]} />
        ))}
      </section>
    </div>
  );
}
