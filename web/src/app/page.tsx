import { ParkCard } from "@/components/park-card";
import { PARKS } from "@/lib/parks";
import { getParkSchedule } from "@/lib/schedule";

// Refresh server-cached schedules every 10 min — same as the per-park
// page. Park hours are stable for the day so this is generous.
export const revalidate = 600;

export default async function HomePage() {
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
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-10">
        {PARKS.map((park, i) => (
          <ParkCard key={park.key} park={park} schedule={schedules[i]} />
        ))}
      </section>
    </div>
  );
}
