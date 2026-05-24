/**
 * Server-only DynamoDB access for the dashboard.
 *
 * Same table the poller Lambda writes. We only read here — the poller
 * is the sole writer for ride state rows. M3 will add write paths
 * (per-user park toggles, favorites) as Next.js Route Handlers in
 * this same app: NextAuth's `auth()` already gives us the Cognito sub
 * in-handler, the SSR compute role grows to include scoped
 * UpdateItem on USER#* and PARK#*#USER#* keys, and TS types stay
 * end-to-end. No separate API service.
 *
 * In dev, the SDK picks up SSO creds via AWS_PROFILE in the shell.
 * In production (Amplify SSR), the SSR compute IAM role provides
 * credentials — no env vars needed. The role is granted via
 * `dataTable.grantReadData(webApp.computeRole)` in disney-stack.ts.
 */
import "server-only";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, ScanCommand, type ScanCommandOutput } from "@aws-sdk/lib-dynamodb";

import type { ParkKey } from "./parks";

// Hardcoded — AWS_REGION is auto-set by the runtime to whatever region
// the SSR Lambda is invoked in, which for Amplify Hosting's edge-style
// SSR can be us-east-1 (CloudFront global). Our DDB table lives in
// us-east-2; reading AWS_REGION here would silently query the wrong
// region with the wrong table. Pin to the table's region.
const region = process.env.DISNEY_REGION ?? "us-east-2";
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
 * The original comment claimed "1 round-trip, well under 4KB" — that
 * was true at M3-era table size (~100 STATE rows total) but stopped
 * being true after M6-B Phase 1 (2026-05-17) started accumulating
 * WAIT# observations. The table grew past one Scan-page (~1MB / ~1000
 * items) and the previous single-page Scan started returning 0 matches
 * for the ~35 STATE rows per park, because the first page contained
 * only WAIT# rows. The live park pages silently rendered "0 attractions"
 * for ~7 days before the bug was caught 2026-05-24.
 *
 * Fix: paginate the Scan, accumulate across all pages. The cost is
 * real (~$0.025-0.04 per page load against the current 425K-item
 * table — within the project's <$5/mo budget at family-scale traffic
 * but unsustainable at any real volume).
 *
 * Right long-term fix: add a GSI on park_key so this becomes a Query
 * (1 round-trip, ~25 items, near-zero cost). Tracked as a deferred
 * follow-up because it requires a CDK change + DDB schema migration
 * and the immediate-pagination fix is enough to unblock production.
 */
export async function getParkRides(parkKey: ParkKey): Promise<RideState[]> {
  const items: RideState[] = [];
  let exclusiveStartKey: Record<string, unknown> | undefined = undefined;
  do {
    const resp: ScanCommandOutput = await client.send(
      new ScanCommand({
        TableName: tableName,
        FilterExpression: "SK = :sk AND park_key = :p",
        ExpressionAttributeValues: { ":sk": "STATE", ":p": parkKey },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...((resp.Items ?? []) as RideState[]));
    exclusiveStartKey = resp.LastEvaluatedKey;
  } while (exclusiveStartKey);
  return items.sort((a, b) => a.name.localeCompare(b.name));
}
