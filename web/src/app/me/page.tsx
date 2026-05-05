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

import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  getUserParkSubscriptions,
  getUserProfile,
} from "@/lib/dynamodb-writes";

import { SettingsForm } from "./settings-form";

// Always render fresh — settings are user-specific and rarely
// hit. No point caching.
export const dynamic = "force-dynamic";

export default async function MePage() {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) {
    // NextAuth's signin endpoint takes callbackUrl so the user
    // lands back on /me after Cognito → Google → callback.
    redirect("/api/auth/signin/cognito?callbackUrl=/me");
  }

  const [profile, subscribedParks] = await Promise.all([
    getUserProfile(sub),
    getUserParkSubscriptions(sub),
  ]);

  return (
    <div className="mx-auto max-w-2xl px-6 py-12">
      <header className="mb-8">
        <p className="label-meta">Your settings</p>
        <h2 className="display text-3xl font-medium mt-2">
          {profile?.name ? `Hi, ${profile.name}.` : "Welcome."}
        </h2>
        <p className="text-fg-2 mt-2 leading-relaxed">
          Set your Pushover key and pick which parks you want alerts for.
          Per-ride favorites land in the next phase.
        </p>
      </header>

      <SettingsForm
        initialName={profile?.name ?? session.user?.name ?? ""}
        initialPushoverUserKey={profile?.pushoverUserKey ?? ""}
        initialSubscribedParks={Array.from(subscribedParks)}
      />
    </div>
  );
}
