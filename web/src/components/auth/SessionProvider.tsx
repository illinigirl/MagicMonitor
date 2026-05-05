"use client";

/**
 * Thin client wrapper around NextAuth's SessionProvider so the root
 * layout (a Server Component) can pass children through without
 * itself becoming "use client". useSession() relies on this provider
 * being mounted somewhere up the tree.
 */

import { SessionProvider as AuthSessionProvider } from "next-auth/react";

export function SessionProvider({ children }: { children: React.ReactNode }) {
  return <AuthSessionProvider>{children}</AuthSessionProvider>;
}
