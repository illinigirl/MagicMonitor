/**
 * /me — per-user settings page (M3 Phase 1).
 *
 * Server-renders the form pre-filled with whatever's in DDB for the
 * signed-in user. Auth gate redirects unauthenticated visitors to
 * the Cognito hosted-UI sign-in flow with /me as the post-login
 * destination.
 *
 * Fetches profile + park subscriptions in parallel — independent
 * partition keys, no ordering constraint, both block render.
 */

import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  getFavoriteRideCountsByPark,
  getUserParkSubscriptions,
  getUserProfile,
} from "@/lib/dynamodb-writes";

import { SettingsForm } from "./settings-form";

// Always render fresh — settings are user-specific and rarely
// hit. No point caching.
export const dynamic = "force-dynamic";

export default async function MePage({
  searchParams,
}: {
  searchParams: Promise<{ welcome?: string }>;
}) {
  const sp = await searchParams;
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) {
    // Redirect to the sign-in PAGE (not the provider endpoint): a GET
    // to /api/auth/signin/cognito has no CSRF token, so Auth.js v5 fails
    // it as a "Configuration" error. The page handles CSRF + the bounce
    // to Cognito → Google, then callbackUrl lands the user back on /me.
    redirect("/api/auth/signin?callbackUrl=/me");
  }

  // Three checks drive the Phase 3 setup banner. Done in parallel
  // — independent partition keys, no ordering dependency. The
  // favorite-counts call also feeds the per-park "(N)" inline count
  // shown next to each park's "Pick favorites →" link.
  const [profile, subscribedParks, favoriteCountsByPark] = await Promise.all([
    getUserProfile(sub),
    getUserParkSubscriptions(sub),
    getFavoriteRideCountsByPark(sub),
  ]);
  const hasFavorites = Object.values(favoriteCountsByPark).some((n) => n > 0);

  const setupStatus = computeSetupStatus({
    hasProfile: profile !== null,
    hasPushoverKey: Boolean(profile?.pushoverUserKey),
    parkCount: subscribedParks.size,
    hasFavorites,
  });

  return (
    <div className="mx-auto max-w-5xl px-6 md:px-10 pb-4">
      <header className="pt-10 mb-8">
        <p className="kicker">Your settings</p>
        <h2 className="display mt-2.5 text-4xl md:text-[52px] leading-[1.05] uppercase text-fg-0">
          {profile?.name
            ? `Hi, ${profile.name}.`
            : sp.welcome === "1"
              ? "Welcome to Magic Monitor."
              : "Welcome."}
        </h2>
        <p className="mt-3 max-w-[640px] text-[15.5px] leading-relaxed text-fg-2">
          Set your Pushover key, pick which parks you want alerts for,
          and choose specific rides to watch.
        </p>
      </header>

      {setupStatus && <SetupBanner status={setupStatus} parks={Array.from(subscribedParks)} />}

      <SettingsForm
        initialName={profile?.name ?? session.user?.name ?? ""}
        initialPushoverUserKey={profile?.pushoverUserKey ?? ""}
        initialSubscribedParks={Array.from(subscribedParks)}
        favoriteCountsByPark={favoriteCountsByPark}
      />
    </div>
  );
}

type SetupStatus =
  | "needs_profile"
  | "needs_parks"
  | "needs_favorites"
  | null;

function computeSetupStatus(state: {
  hasProfile: boolean;
  hasPushoverKey: boolean;
  parkCount: number;
  hasFavorites: boolean;
}): SetupStatus {
  if (!state.hasProfile || !state.hasPushoverKey) return "needs_profile";
  if (state.parkCount === 0) return "needs_parks";
  if (!state.hasFavorites) return "needs_favorites";
  return null;
}

/**
 * Adaptive copy block at the top of /me. Tells signed-in users
 * what's still missing before alerts will actually fire. Disappears
 * once profile + Pushover key + at least one park sub + at least
 * one favorite are in place. Order of priority:
 *   1. profile / pushover key — without these, nothing else matters
 *   2. park subscriptions     — favorites without subs do nothing
 *   3. favorites              — final piece of the chain
 */
function SetupBanner({
  status,
  parks,
}: {
  status: NonNullable<SetupStatus>;
  parks: string[];
}) {
  let title: string;
  let body: React.ReactNode;

  if (status === "needs_profile") {
    title = "First time here?";
    body = (
      <>
        Add your name and Pushover user key below to start receiving alerts.
        You can grab your key at{" "}
        <a
          href="https://pushover.net/"
          target="_blank"
          rel="noreferrer"
          className="underline hover:text-fg-0"
        >
          pushover.net
        </a>{" "}
        after creating an account.
      </>
    );
  } else if (status === "needs_parks") {
    title = "Pick your parks";
    body = <>Check at least one park below so we know where to watch for ride status changes.</>;
  } else {
    // needs_favorites — list the parks they're subscribed to so the
    // "Pick favorites" links below feel less abstract.
    title = "One more step — pick favorite rides";
    body = (
      <>
        You&apos;re subscribed to {parks.length === 1 ? "one park" : `${parks.length} parks`}{" "}
        but haven&apos;t picked any favorite rides yet — so no alerts will fire. Use the{" "}
        <span className="text-fg-1">&ldquo;Pick favorites →&rdquo;</span> link next to each park
        below to choose which rides matter to you.
      </>
    );
  }

  return (
    <div className="mb-8 flex items-start gap-4 rounded-md border-2 border-warn bg-warn-bg px-[22px] py-[18px]">
      <span
        className="display flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full bg-warn text-[15px] text-bg-0"
        aria-hidden
      >
        !
      </span>
      <div>
        <p className="font-head font-semibold text-[15px] uppercase tracking-[0.08em] text-fg-0">
          {title}
        </p>
        <p className="mt-1 text-[13.5px] leading-relaxed text-fg-2">{body}</p>
      </div>
    </div>
  );
}
