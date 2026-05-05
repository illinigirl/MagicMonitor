import { NextResponse } from "next/server";

/**
 * TEMPORARY — no AWS SDK imports, just dumps env vars. If this 500s
 * too, the SSR runtime itself is broken. If only /api/debug/ddb 500s,
 * the AWS SDK import is the problem.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json({
    ok: true,
    env: {
      AWS_REGION: process.env.AWS_REGION ?? null,
      AWS_DEFAULT_REGION: process.env.AWS_DEFAULT_REGION ?? null,
      DISNEY_REGION: process.env.DISNEY_REGION ?? null,
      DISNEY_TABLE_NAME: process.env.DISNEY_TABLE_NAME ?? null,
      AWS_LAMBDA_FUNCTION_NAME: process.env.AWS_LAMBDA_FUNCTION_NAME ?? null,
      AWS_EXECUTION_ENV: process.env.AWS_EXECUTION_ENV ?? null,
      AWS_LAMBDA_FUNCTION_VERSION: process.env.AWS_LAMBDA_FUNCTION_VERSION ?? null,
      HAS_AWS_ACCESS_KEY_ID: Boolean(process.env.AWS_ACCESS_KEY_ID),
      HAS_AWS_SESSION_TOKEN: Boolean(process.env.AWS_SESSION_TOKEN),
      HAS_AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: Boolean(
        process.env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI,
      ),
      NODE_ENV: process.env.NODE_ENV ?? null,
      NEXT_RUNTIME: process.env.NEXT_RUNTIME ?? null,
    },
    timestamp: new Date().toISOString(),
  });
}
