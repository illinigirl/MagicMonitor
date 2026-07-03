/**
 * Shared "not on the family allowlist" state for family-gated pages
 * (/trips, /replan). Critically, it offers a SIGN-OUT — without it, a
 * wrong-account sign-in (e.g. a passkey grabbing a non-family Google
 * account) is a dead end: the page shows "family only" with no way to
 * switch. Sign-out does the full Cognito federated logout so the next
 * sign-in shows the account picker instead of silently re-authing the
 * same wrong account.
 */

import { SignOutButton } from "@/components/auth/SignOutButton";

export function FamilyOnly({ email }: { email?: string | null }) {
  return (
    <div className="mx-auto max-w-md px-6 py-16 text-center">
      <p className="label-meta">Trips</p>
      <h2 className="display text-2xl font-medium mt-2">Family accounts only</h2>
      <p className="text-fg-2 leading-relaxed mt-3">
        The trip planner is a shared family space. You&rsquo;re signed in as{" "}
        <span className="text-fg-1">{email ?? "an unknown account"}</span>,
        which isn&rsquo;t on the family list.
      </p>
      <p className="text-fg-3 text-sm mt-2">
        Signed in with the wrong account? Sign out and pick your family one.
      </p>
      <div className="mt-6">
        <SignOutButton className="inline-flex items-center gap-2 rounded-md bg-gold px-4 py-2 text-sm font-medium text-gold-ink hover:opacity-90">
          Sign out &amp; switch account
        </SignOutButton>
      </div>
    </div>
  );
}
