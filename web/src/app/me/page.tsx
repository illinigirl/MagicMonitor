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
  getUserParkSubscriptions,
  getUserProfile,
  userHasAnyFavorites,
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
    // NextAuth's signin endpoint takes callbackUrl so the user
    // lands back on /me after Cognito → Google → callback.
    redirect("/api/auth/signin/cognito?callbackUrl=/me");
  }

  // Three checks drive the Phase 3 setup banner. Done in parallel
  // — independent partition keys, no ordering dependency.
  const [profile, subscribedParks, hasFavorites] = await Promise.all([
    getUserProfile(sub),
    getUserParkSubscriptions(sub),
    userHasAnyFavorites(sub),
  ]);

  const setupStatus = computeSetupStatus({
    hasProfile: profile !== null,
    hasPushoverKey: Boolean(profile?.pushoverUserKey),
    parkCount: subscribedParks.size,
    hasFavorites,
  });

  return (
    <div className="mx-auto max-w-2xl px-6 py-12">
      <header className="mb-8">
        <p className="label-meta">Your settings</p>
        <h2 className="display text-3xl font-medium mt-2">
          {profile?.name
            ? `Hi, ${profile.name}.`
            : sp.welcome === "1"
              ? "Welcome to Magic Monitor."
              : "Welcome."}
        </h2>
        <p className="text-fg-2 mt-2 leading-relaxed">
          Set your Pushover key, pick which parks you want alerts for,
          and choose specific rides to watch.
        </p>
      </header>

      {setupStatus && <SetupBanner status={setupStatus} parks={Array.from(subscribedParks)} />}

      <SettingsForm
        initialName={profile?.name ?? session.user?.name ?? ""}
        initialPushoverUserKey={profile?.pushoverUserKey ?? ""}
        initialSubscribedParks={Array.from(subscribedParks)}
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
    <div className="mb-6 rounded-lg border border-gold/30 bg-gold/5 px-4 py-3">
      <p className="text-fg-0 font-medium text-sm">{title}</p>
      <p className="text-fg-2 text-sm mt-1 leading-relaxed">{body}</p>
    </div>
  );
}
