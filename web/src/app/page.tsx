import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ParkCard } from "@/components/park-card";
import { RetroHeatmap } from "@/components/retro-heatmap";
import { DiamondRule } from "@/components/retro";
import { WeatherChip } from "@/components/weather-chip";
import { getParkHeatmap } from "@/lib/analytics";
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
  // copy of the setup banner. Bypass the redirect with ?welcome=skipped.
  if (session?.user?.id && sp.welcome !== "skipped") {
    const profile = await getUserProfile(session.user.id);
    if (!profile) {
      redirect("/me?welcome=1");
    }
  }

  // Fetch all 4 schedules in parallel so the cards render hours+status
  // without four sequential round trips.
  const schedules = await Promise.all(PARKS.map((p) => getParkSchedule(p.key)));

  // MK heatmap for the "when the lines are long" teaser; the full
  // per-park version lives on /parks/<park>/analytics.
  const mkHeatmap = getParkHeatmap("magic_kingdom");

  return (
    <div className="mx-auto max-w-6xl px-6 md:px-10 pb-4">
      <section className="pt-11 text-center">
        <h2 className="display text-[40px] md:text-[68px] leading-[1.05] text-fg-0">
          PICK A PARK
        </h2>
        <DiamondRule />
        <p className="mx-auto mt-4 max-w-[640px] text-base leading-relaxed text-fg-2">
          Wait times and ride status update every two minutes. Down rides
          surface to the top so you don&apos;t have to scan for them.
        </p>
        <div className="mt-5">
          <WeatherChip />
        </div>
      </section>

      <section className="mt-8 grid grid-cols-1 gap-5 md:grid-cols-2">
        {PARKS.map((park, i) => (
          <ParkCard key={park.key} park={park} schedule={schedules[i]} />
        ))}
      </section>

      <section className="mt-9 border-t-2 border-line pt-6">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="head text-xl">When the lines are long</h3>
          <Link
            href="/analytics"
            className="poster-link text-accent hover:underline"
          >
            Full analytics →
          </Link>
        </div>
        <p className="mb-4 mt-1.5 text-[13px] text-fg-2">
          Magic Kingdom · average wait by hour and day. Darker red = longer
          waits.
        </p>
        <RetroHeatmap cells={mkHeatmap} />
      </section>
    </div>
  );
}
