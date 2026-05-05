/**
 * Type augmentation for NextAuth (Auth.js v5).
 *
 * We surface `id` on session.user (= Cognito sub) so server and
 * client components can key per-user data off it without per-call
 * type assertions. M3 will rely on this everywhere it touches
 * USER#<sub> rows in DynamoDB.
 */

import "next-auth";

declare module "next-auth" {
  interface Session {
    user?: {
      id: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
    };
  }
}
