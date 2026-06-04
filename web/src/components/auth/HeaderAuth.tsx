/**
 * Server component that decides which auth control to show in the
 * site header. Calls `auth()` directly on the server so the initial
 * page render is correct without a flash of "signed-out" UI on
 * hydration. Reading session server-side also avoids a useSession()
 * roundtrip on every navigation.
 *
 * Email is shown next to the Sign-out button as a sanity check —
 * "I'm signed in as the right account" — without going to a profile
 * page. M3 will replace this with a profile-link affordance.
 */

import Link from "next/link";

import { auth } from "@/auth";
import { isTripsAllowed } from "@/lib/trips-access";

import { SignInButton } from "./SignInButton";
import { SignOutButton } from "./SignOutButton";

export async function HeaderAuth() {
  const session = await auth();

  if (!session?.user) {
    return <SignInButton callbackUrl="/">Sign in</SignInButton>;
  }

  const email = session.user.email ?? "";
  // Only family accounts see the Trips link — /trips shows shared data
  // and denies non-family, so don't surface a dead-end link to others.
  const showTrips = isTripsAllowed(session.user.email);

  return (
    <div className="flex items-center gap-3">
      {email && (
        <span className="hidden md:inline text-fg-3 text-sm" title={email}>
          {email}
        </span>
      )}
      {showTrips && (
        <Link
          href="/trips"
          className="rounded-md border border-line bg-bg-1 hover:bg-bg-2 px-3 py-1.5 text-sm font-medium text-fg-1 transition-colors"
        >
          Trips
        </Link>
      )}
      <Link
        href="/me"
        className="rounded-md border border-line bg-bg-1 hover:bg-bg-2 px-3 py-1.5 text-sm font-medium text-fg-1 transition-colors"
      >
        Settings
      </Link>
      <SignOutButton />
    </div>
  );
}
