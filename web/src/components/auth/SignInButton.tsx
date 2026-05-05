"use client";

/**
 * Sign-in CTA. Submits a plain HTML form POST to NextAuth's signin
 * endpoint, which responds 302 + Set-Cookie (the PKCE/nonce/state
 * cookies the callback needs to validate the OIDC response).
 *
 * Why not next-auth/react's signIn() helper: that client function
 * adds an `x-auth-return-redirect: 1` header which makes NextAuth
 * return a 200 JSON response for client-side navigation. In that
 * mode, the PKCE/state cookies don't reach the browser (observed
 * on Watchtower with NextAuth v5.0.0-beta.31 + Amplify SSR + Chrome)
 * and the callback fails with "Configuration" because state and
 * code-verifier are missing. Plain form POST avoids the problem.
 *
 * CSRF: fetched at submit-time, not mount-time. NextAuth's
 * /api/auth/csrf endpoint rotates the cookie on every call — when
 * SessionProvider or another component independently triggers a
 * second csrf call, a cached mount-time token desyncs from the
 * cookie and the POST fails MissingCSRF. Submit-time fetch
 * guarantees body token and cookie match.
 */

import { useRef, useState } from "react";

interface Props {
  callbackUrl?: string;
  className?: string;
  children?: React.ReactNode;
}

export function SignInButton({
  callbackUrl = "/",
  className,
  children = "Sign in",
}: Props) {
  const formRef = useRef<HTMLFormElement>(null);
  const csrfInputRef = useRef<HTMLInputElement>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const r = await fetch("/api/auth/csrf");
      const j = (await r.json()) as { csrfToken: string };
      if (csrfInputRef.current) csrfInputRef.current.value = j.csrfToken;
      // Native submit, bypassing React's submit handler — sends the
      // form as a regular POST so the browser does the redirect dance
      // (Set-Cookie for PKCE/nonce/state lands intact).
      formRef.current?.submit();
    } catch {
      setSubmitting(false);
    }
  }

  return (
    <form
      ref={formRef}
      method="POST"
      action="/api/auth/signin/cognito"
      onSubmit={handleSubmit}
      style={{ display: "inline" }}
    >
      <input ref={csrfInputRef} type="hidden" name="csrfToken" value="" />
      <input type="hidden" name="callbackUrl" value={callbackUrl} />
      <button
        type="submit"
        disabled={submitting}
        className={
          className ??
          "inline-flex items-center gap-2 rounded-md border border-line bg-bg-1 hover:bg-bg-2 px-3 py-1.5 text-sm font-medium text-fg-0 transition-colors disabled:opacity-60"
        }
      >
        {children}
      </button>
    </form>
  );
}
