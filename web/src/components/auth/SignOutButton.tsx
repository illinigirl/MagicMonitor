"use client";

/**
 * Sign-out flow. NextAuth clears its own session cookie via
 * `signOut()`. We then redirect through Cognito's hosted-UI
 * /logout endpoint so Cognito itself drops its session — without
 * that step, the next sign-in attempt would silently re-use the
 * previous Cognito session and skip the Google account-picker.
 *
 * The logout_uri redirect target ("/" on this origin) must be
 * whitelisted in the UserPoolClient — done in disney-stack.ts.
 */

import { signOut } from "next-auth/react";

const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN_URL;
const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID;

export function SignOutButton({
  className,
  children = "Sign out",
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  const onClick = async () => {
    // 1. Drop the local NextAuth cookie. `redirect: false` keeps us
    //    in this tab so we can issue the Cognito logout next.
    await signOut({ redirect: false });

    // 2. Bounce through Cognito's hosted-UI logout. Without this,
    //    Cognito's own session cookie persists and a "Sign in"
    //    click silently re-authenticates the same user without a
    //    Google prompt — confusing UX for shared browsers.
    if (COGNITO_DOMAIN && COGNITO_CLIENT_ID && typeof window !== "undefined") {
      const params = new URLSearchParams({
        client_id: COGNITO_CLIENT_ID,
        logout_uri: `${window.location.origin}/`,
      });
      window.location.href = `${COGNITO_DOMAIN}/logout?${params}`;
    } else {
      // Misconfigured env — fall back to a same-origin redirect so
      // the local session is at least cleared.
      window.location.href = "/";
    }
  };

  return (
    <button
      type="button"
      onClick={onClick}
      className={
        className ??
        "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-fg-2 hover:text-fg-0 hover:bg-bg-2 transition-colors"
      }
    >
      {children}
    </button>
  );
}
