import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Server components import the AWS SDK directly. Listing it here
  // tells Next not to bundle it (it's already a peer dep) and avoids
  // the "node:crypto can't be bundled" warnings during build.
  serverExternalPackages: [
    "@aws-sdk/client-dynamodb",
    "@aws-sdk/lib-dynamodb",
  ],
};

export default nextConfig;
