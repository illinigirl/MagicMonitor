import { NextResponse } from "next/server";
import {
  DynamoDBClient,
  ScanCommand,
} from "@aws-sdk/client-dynamodb";

/**
 * TEMPORARY debug endpoint — surfaces SSR-side DDB errors as JSON so
 * we can diagnose without CloudWatch logs (Amplify Hosting's SSR
 * Lambda runs in AWS-managed infrastructure with no customer-side
 * log access). Remove once the SSR → DDB connection is verified.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  const region = process.env.DISNEY_REGION ?? "us-east-2";
  const tableName = process.env.DISNEY_TABLE_NAME ?? "DisneyData";
  const envSnapshot = {
    AWS_REGION: process.env.AWS_REGION ?? null,
    AWS_DEFAULT_REGION: process.env.AWS_DEFAULT_REGION ?? null,
    DISNEY_REGION: process.env.DISNEY_REGION ?? null,
    DISNEY_TABLE_NAME: process.env.DISNEY_TABLE_NAME ?? null,
    AWS_LAMBDA_FUNCTION_NAME: process.env.AWS_LAMBDA_FUNCTION_NAME ?? null,
    AWS_EXECUTION_ENV: process.env.AWS_EXECUTION_ENV ?? null,
    HAS_AWS_ACCESS_KEY_ID: Boolean(process.env.AWS_ACCESS_KEY_ID),
    HAS_AWS_SESSION_TOKEN: Boolean(process.env.AWS_SESSION_TOKEN),
  };

  try {
    const client = new DynamoDBClient({ region });
    const resp = await client.send(
      new ScanCommand({ TableName: tableName, Limit: 1 }),
    );
    return NextResponse.json({
      ok: true,
      env: envSnapshot,
      tableName,
      region,
      itemCount: resp.Count,
      scannedCount: resp.ScannedCount,
    });
  } catch (err) {
    const e = err as Error & { name?: string; $metadata?: unknown };
    return NextResponse.json({
      ok: false,
      env: envSnapshot,
      tableName,
      region,
      errorName: e.name,
      errorMessage: e.message,
      metadata: e.$metadata,
    });
  }
}
