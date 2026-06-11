/**
 * Family allowlist for the shared /trips surface.
 *
 * /trips shows the SHARED trip space (USER#megan) the MCP planner writes
 * to — so unlike per-user pages it must be gated to the family, not just
 * "any logged-in user." Email-based (you know the family's emails;
 * Cognito subs are opaque). Configure via the TRIPS_ALLOWED_EMAILS env
 * var (comma-separated) on the Amplify SSR app — add/remove family with
 * no code change. Unset / empty → deny-all (safe default).
 *
 * Server-side only (reads a non-public env var); used by the /trips page
 * gate and the conditional nav link.
 */
import "server-only";

// Trust model (reviewed 2026-06-11): this gates on the email string
// alone, without an explicit email_verified assertion. That's accepted
// because the only identity source is Cognito federated to Google with
// scope "openid email profile" (see web/src/auth.ts) — Google verifies
// the email, so a session.user.email reaching here is already verified in
// practice. If a non-Google IdP (or an unverified-email IdP) is ever added
// to the pool, this gate must additionally require an email_verified claim
// before trusting the address.
export function isTripsAllowed(email: string | null | undefined): boolean {
  if (!email) return false;
  const allowed = (process.env.TRIPS_ALLOWED_EMAILS ?? "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
  return allowed.includes(email.toLowerCase());
}
