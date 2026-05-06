/**
 * NextAuth (Auth.js v5) configuration. Sole identity provider is the
 * Watchtower Cognito user pool, which itself federates to Google.
 *
 * Magic Monitor's auth is intentionally minimal vs Watchtower's setup:
 * we don't plumb the Cognito access_token through, because MM's pages
 * read DynamoDB directly via the Amplify SSR Lambda's IAM role rather
 * than calling an external API as a bearer-authenticated client. The
 * Cognito session here only establishes WHO the user is (sub +
 * email) — M3 will key per-user data off `session.user.id` (= sub)
 * but doesn't need the OIDC access token to do so. If MM ever adds
 * a bearer-authenticated API tier we'd port the refresh logic from
 * Watchtower.
 *
 * The Cognito provider uses a confidential client (clientSecret set).
 * NextAuth v5's Cognito provider validates clientSecret presence
 * before any client.token_endpoint_auth_method override would take
 * effect, so a public-client / PKCE-only setup isn't workable here.
 * The secret never reaches the browser — it's used server-side by
 * the SSR Lambda when exchanging the auth code for tokens.
 */

import NextAuth, { type NextAuthConfig } from "next-auth";
import Cognito from "next-auth/providers/cognito";

export const config: NextAuthConfig = {
  providers: [
    Cognito({
      // The Cognito provider derives all endpoints from `issuer` via
      // the OIDC discovery doc at /.well-known/openid-configuration.
      // Note: issuer is the user-pool URL, NOT the hosted-UI domain.
      issuer: process.env.COGNITO_ISSUER,
      clientId: process.env.COGNITO_CLIENT_ID,
      clientSecret: process.env.COGNITO_CLIENT_SECRET,
      // Cognito requires client_secret_basic for the token endpoint;
      // Auth.js v5's default (client_secret_post) gets back
      // invalid_client.
      client: { token_endpoint_auth_method: "client_secret_basic" },
      // Cognito always emits a `nonce` claim in id_tokens. Auth.js v5
      // strictly validates it against a client-stored nonce cookie —
      // if absent, validation throws and surfaces as a generic
      // "Configuration" error on first sign-in. Enabling the nonce
      // check makes Auth.js generate, send, store, and validate one.
      checks: ["pkce", "nonce"],
      // Skip Cognito's own login form and bounce straight to Google.
      // Single-IdP UX: one click to sign in. `prompt=select_account`
      // forces Google's account picker on each sign-in (helpful for
      // demos where multiple test users share a browser).
      authorization: {
        params: {
          identity_provider: "Google",
          scope: "openid email profile",
          prompt: "select_account",
        },
      },
    }),
  ],
  // Encrypted-JWT session, stored in an httpOnly cookie. No DB.
  session: { strategy: "jwt" },
  callbacks: {
    // Initial sign-in carries `account` + `profile`; subsequent
    // requests only carry `token` (JWT decoded from the cookie).
    //
    // Why this exists (M3 Phase 2 bug): without an explicit override,
    // Auth.js v5's JWT-strategy session was emitting a fresh random
    // sub per sign-in instead of the Cognito user's stable sub. We
    // discovered this when three sign-ins by the same user produced
    // three different USER#<sub> partitions in DynamoDB. Anchoring
    // `token.sub` to Cognito's ID-token sub on each sign-in fixes
    // it: subsequent JWT-only refreshes carry the same sub via the
    // cookie, and any future sign-in re-anchors to the same value.
    async jwt({ token, account, profile }) {
      if (
        account?.provider === "cognito" &&
        typeof profile?.sub === "string"
      ) {
        token.sub = profile.sub;
      }
      return token;
    },
    async session({ session, token }) {
      // Surface the Cognito sub on session.user.id. M3 uses this as
      // the partition-key prefix for per-user DDB rows (USER#<sub>).
      if (session.user) {
        session.user.id = (token.sub as string | undefined) ?? "";
      }
      return session;
    },
  },
};

export const { handlers, signIn, signOut, auth } = NextAuth(config);
