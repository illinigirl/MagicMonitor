import type { NextConfig } from "next";

/**
 * Next.js config.
 *
 * Originally we listed the AWS SDK packages under `serverExternalPackages`
 * to skip bundling them. That worked locally but failed in Amplify's SSR
 * Lambda with "Cannot find module @aws-sdk/client-dynamodb-<hash>" — a
 * Turbopack-bundled require that points at a hashed module name which
 * doesn't exist in pnpm's nested `.pnpm/<pkg>@<ver>/node_modules/...`
 * layout at runtime. Removing them from the externals list makes
 * Turbopack bundle the SDK inline, which eliminates the runtime resolve
 * entirely. Bundle size grows by ~600KB which is fine for a Lambda
 * SSR target.
 */
const nextConfig: NextConfig = {};

export default nextConfig;
