/**
 * Server-only DynamoDB access for the dashboard.
 *
 * Same table the poller Lambda writes. We only read here — the poller
 * is the sole writer for ride state rows. Future M2-B work will add
 * write paths (per-user park toggles, favorites) but keep them in
 * separate API routes that go through API Gateway + a FastAPI Lambda
 * to match Watchtower's pattern.
 *
 * In dev, the SDK picks up SSO creds via AWS_PROFILE in the shell.
 * In production (Amplify SSR), the IAM role provides credentials —
 * no env vars needed.
 */
import "server-only";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, ScanCommand } from "@aws-sdk/lib-dynamodb";

import type { ParkKey } from "./parks";

const region = process.env.AWS_REGION ?? "us-east-2";
const tableName = process.env.DISNEY_TABLE_NAME ?? "DisneyData";

// One client per Node process — Next.js dev hot-reloads modules so
// we need a global cache to avoid leaking sockets on every reload.
declare global {
  // eslint-disable-next-line no-var
  var __ddbClient: DynamoDBDocumentClient | undefined;
}

const client =
  globalThis.__ddbClient ??
  DynamoDBDocumentClient.from(new DynamoDBClient({ region }), {
    marshallOptions: { removeUndefinedValues: true },
  });
if (process.env.NODE_ENV !== "production") globalThis.__ddbClient = client;

export type RideStatus = "OPERATING" | "DOWN" | "CLOSED" | "REFURBISHMENT";

export interface RideState {
  ride_id: string;
  park_key: ParkKey;
  park_name: string;
  name: string;
  status: RideStatus;
  wait_mins: number | null;
  last_seen: string;
  ll: { type: "paid" | "free"; price?: string; return_start?: string } | null;
}

/**
 * Scan all current STATE rows for one park.
 *
 * At ~25 rides per park this is 1 round-trip and well under 4KB —
 * cheaper than maintaining a GSI. M4 (analytics) will likely add
 * a GSI on park_key but day-to-day live data doesn't need it.
 */
export async function getParkRides(parkKey: ParkKey): Promise<RideState[]> {
  const resp = await client.send(
    new ScanCommand({
      TableName: tableName,
      FilterExpression: "SK = :sk AND park_key = :p",
      ExpressionAttributeValues: { ":sk": "STATE", ":p": parkKey },
    }),
  );
  return ((resp.Items ?? []) as RideState[]).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}
