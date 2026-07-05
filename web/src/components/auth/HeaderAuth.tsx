/**
 * Server component that decides which auth control to show in the
 * masthead nav. Calls `auth()` directly on the server so the initial
 * page render is correct without a flash of "signed-out" UI on
 * hydration. Reading session server-side also avoids a useSession()
 * roundtrip on every navigation.
 *
 * Signed in: TRIPS (family only) / SETTINGS / SIGN OUT in Oswald caps
 * — active route underlined in red-orange via NavItem. Signed out:
 * a single SIGN IN control in the same nav style.
 */

import { auth } from "@/auth";
import { isTripsAllowed } from "@/lib/trips-access";

import { NavItem } from "@/components/nav-item";
import { SignInButton } from "./SignInButton";
import { SignOutButton } from "./SignOutButton";

// Oswald caps treatment for the sign in/out controls, matching NavItem.
// SIGN OUT is the red-orange nav item per the poster design.
const NAV_ACTION_CLASSES =
  "font-head font-semibold text-[13px] uppercase tracking-[0.18em] " +
  "text-accent hover:text-fg-0 transition-colors duration-100 cursor-pointer";

export async function HeaderAuth() {
  const session = await auth();

  if (!session?.user) {
    return (
      <SignInButton callbackUrl="/" className={NAV_ACTION_CLASSES}>
        Sign in
      </SignInButton>
    );
  }

  const email = session.user.email ?? "";
  // Only family accounts see the Trips link — /trips shows shared data
  // and denies non-family, so don't surface a dead-end link to others.
  const showTrips = isTripsAllowed(session.user.email);

  return (
    <div className="flex items-center gap-[26px]">
      {email && (
        <span
          className="hidden md:inline text-fg-3 text-xs font-ui"
          title={email}
        >
          {email}
        </span>
      )}
      {showTrips && <NavItem href="/trips">Trips</NavItem>}
      <NavItem href="/me">Settings</NavItem>
      <SignOutButton className={NAV_ACTION_CLASSES}>Sign out</SignOutButton>
    </div>
  );
}
