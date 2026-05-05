import Link from "next/link";

import { HeaderAuth } from "@/components/auth/HeaderAuth";

/**
 * Top-of-page header — appears on every route. Title links home,
 * subtitle is a one-liner identity statement, and the right side
 * is the auth control (Sign in when signed out, email + Sign out
 * when signed in). Pages stay public for M2-B; M3 will gate the
 * per-user pages by reading auth() in those routes.
 */
export function SiteHeader() {
  return (
    <header className="border-b border-line-soft">
      <div className="mx-auto max-w-6xl px-6 py-5 flex items-baseline justify-between gap-6">
        <Link href="/" className="group">
          <h1 className="display text-3xl font-semibold tracking-tight">
            <span className="text-fg-0">Magic</span>{" "}
            <span className="text-gold">Monitor</span>
          </h1>
        </Link>
        <div className="flex items-center gap-6">
          <p className="hidden lg:block text-fg-2 text-sm">
            Live ride status across the four Walt Disney World parks
          </p>
          <HeaderAuth />
        </div>
      </div>
    </header>
  );
}
