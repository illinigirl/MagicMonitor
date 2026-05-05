import Link from "next/link";

/**
 * Top-of-page header — appears on every route. Title links home,
 * subtitle is a one-liner identity statement. M2-B will add a sign-in
 * button on the right; M2 leaves it minimal.
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
        <p className="hidden sm:block text-fg-2 text-sm">
          Live ride status across the four Walt Disney World parks
        </p>
      </div>
    </header>
  );
}
