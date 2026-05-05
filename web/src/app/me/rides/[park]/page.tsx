/**
 * /me/rides/[park] — pick favorite rides for one park (M3 Phase 2).
 *
 * Auth-gated; redirects to sign-in with this URL as the post-login
 * destination. Ride list comes from the same RIDE# STATE rows the
 * /parks/[park] page reads — single source of truth means a new
 * ride that appears in DDB shows up here on the next request.
 *
 * The page also surfaces each ride's current operating status as a
 * small badge so users can favorite knowing which rides are actually
 * running today.
 */

import { notFound, redirect } from "next/navigation";
import Link from "next/link";

import { auth } from "@/auth";
import { getParkRides } from "@/lib/dynamodb";
import { getUserFavoriteRides } from "@/lib/dynamodb-writes";
import { findPark } from "@/lib/parks";

import { FavoritesForm } from "./favorites-form";

export const dynamic = "force-dynamic";

export default async function FavoritesPage({
  params,
}: {
  params: Promise<{ park: string }>;
}) {
  const { park: parkKeyRaw } = await params;
  const park = findPark(parkKeyRaw);
  if (!park) notFound();

  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) {
    redirect(
      `/api/auth/signin/cognito?callbackUrl=/me/rides/${park.key}`,
    );
  }

  // Parallel: full ride list + this user's existing favorites.
  const [rides, favorites] = await Promise.all([
    getParkRides(park.key),
    getUserFavoriteRides(sub, park.key),
  ]);

  // Show DOWN rides first so they're easy to deselect, then OPERATING,
  // then everything else. Mirrors the /parks page's instinct: surface
  // what changed first.
  const sorted = [...rides].sort((a, b) => {
    const order: Record<string, number> = {
      DOWN: 0,
      OPERATING: 1,
      REFURBISHMENT: 2,
      CLOSED: 3,
    };
    const oa = order[a.status] ?? 99;
    const ob = order[b.status] ?? 99;
    if (oa !== ob) return oa - ob;
    return a.name.localeCompare(b.name);
  });

  return (
    <div
      className="mx-auto max-w-3xl px-6 py-12"
      style={
        { "--park-accent": `var(${park.accentVar})` } as React.CSSProperties
      }
    >
      <header className="mb-8">
        <Link
          href="/me"
          className="text-fg-3 hover:text-fg-1 text-sm transition-colors"
        >
          ← Settings
        </Link>
        <div className="flex items-baseline gap-3 mt-3">
          <h2 className="display text-3xl font-medium">
            Favorites · {park.name}
          </h2>
          <span
            className="h-1 w-12 rounded-full"
            style={{ background: "var(--park-accent)" }}
          />
        </div>
        <p className="text-fg-2 mt-3 leading-relaxed">
          Check the rides you want alerts for. You&apos;ll only get a
          push when one of these specific rides changes status — no
          alerts about other rides in the park.
        </p>
        <p className="label-meta mt-3">
          {rides.length} attractions · {favorites.size} currently favorited
        </p>
      </header>

      {rides.length === 0 ? (
        <p className="text-fg-2">
          No rides found for {park.name} yet. The poller may still be
          fetching the first batch.
        </p>
      ) : (
        <FavoritesForm
          parkKey={park.key}
          rides={sorted.map((r) => ({
            ride_id: r.ride_id,
            name: r.name,
            status: r.status,
          }))}
          initialFavorites={Array.from(favorites)}
        />
      )}
    </div>
  );
}
