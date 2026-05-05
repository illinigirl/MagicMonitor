/**
 * Server-only DynamoDB writes for per-user data (M3).
 *
 * Sibling to `dynamodb.ts` (the read path). Kept in a separate file
 * so the write surface — and its IAM scope — is grep-able in one
 * place. The Amplify SSR compute role is granted PutItem / UpdateItem
 * / DeleteItem ONLY on partitions whose PK starts with `USER#` or
 * `PARK#` (see disney-stack.ts "M3: scoped write permissions"). Any
 * write here that targets a different prefix will fail at IAM with
 * AccessDeniedException — that's intentional defense-in-depth.
 *
 * IMPORTANT — cross-user isolation is enforced HERE, not in IAM. All
 * SSR requests run as the same compute role, so the role can write
 * to any USER#* PK. Every function in this module takes `sub` as the
 * first arg and the caller must pass the value from `auth().user.id`
 * (the Cognito sub for the current session). Never accept `sub` from
 * the request body.
 */
import "server-only";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  PutCommand,
  UpdateCommand,
  DeleteCommand,
  GetCommand,
  ScanCommand,
  QueryCommand,
} from "@aws-sdk/lib-dynamodb";

import type { ParkKey } from "./parks";

// Same region/table conventions as dynamodb.ts. AWS_REGION is
// unreliable on Amplify SSR (see comment in dynamodb.ts), so we pin
// to us-east-2 where the table lives.
const region = process.env.DISNEY_REGION ?? "us-east-2";
const tableName = process.env.DISNEY_TABLE_NAME ?? "DisneyData";

// Reuse the same global singleton key as the reader so dev hot-reload
// doesn't multiply the socket pool. In production each module
// initializes its own reference, but both end up pointing at the
// first-constructed client via the global cache.
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

// ─── Profile ─────────────────────────────────────────────────────────

export interface UserProfile {
  /** Cognito sub — never leaves the server. */
  sub: string;
  /** Display name shown on the dashboard. */
  name: string;
  /** Pushover user key (validated against api.pushover.net before save). */
  pushoverUserKey: string;
  /** ISO-8601 timestamp of the last write. */
  updatedAt: string;
}

/**
 * Upsert the per-user profile row at `USER#<sub>/PROFILE`.
 *
 * UpdateItem (rather than PutItem) so future fields added to the
 * profile row by other code paths aren't clobbered on save. The
 * poller already writes nothing under USER#<sub>, but defending
 * against hypothetical future writers is cheap.
 */
export async function putUserProfile(
  sub: string,
  fields: { name: string; pushoverUserKey: string },
): Promise<void> {
  const updatedAt = new Date().toISOString();
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
      // `name` is a DDB reserved word, hence the #name alias.
      UpdateExpression:
        "SET #name = :name, pushover_user_key = :pk, updated_at = :ts",
      ExpressionAttributeNames: { "#name": "name" },
      ExpressionAttributeValues: {
        ":name": fields.name,
        ":pk": fields.pushoverUserKey,
        ":ts": updatedAt,
      },
    }),
  );
}

interface UserProfileRow {
  PK: string;
  SK: string;
  name?: string;
  pushover_user_key?: string;
  updated_at?: string;
}

/** Read the per-user profile row. Used by the /me page to pre-fill the form. */
export async function getUserProfile(
  sub: string,
): Promise<UserProfile | null> {
  const resp = await client.send(
    new GetCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
    }),
  );
  const item = resp.Item as UserProfileRow | undefined;
  if (!item) return null;
  return {
    sub,
    name: item.name ?? "",
    pushoverUserKey: item.pushover_user_key ?? "",
    updatedAt: item.updated_at ?? "",
  };
}

// ─── Park subscriptions ──────────────────────────────────────────────

/**
 * Toggle a park subscription on or off.
 *
 * Subscribed=true: PutItem `PARK#<key>/USER#<sub>` with subscribed_at.
 * Subscribed=false: DeleteItem the same row.
 *
 * Both operations are idempotent — calling them twice produces the
 * same end state. The poller's fanout query (Query on PK=PARK#<key>,
 * SK begins_with USER#) picks up the change on its next 2-min tick.
 */
export async function setParkSubscription(
  sub: string,
  parkKey: ParkKey,
  subscribed: boolean,
): Promise<void> {
  const Key = { PK: `PARK#${parkKey}`, SK: `USER#${sub}` };
  if (subscribed) {
    await client.send(
      new PutCommand({
        TableName: tableName,
        Item: { ...Key, subscribed_at: new Date().toISOString() },
      }),
    );
  } else {
    await client.send(
      new DeleteCommand({ TableName: tableName, Key }),
    );
  }
}

interface ParkSubRow {
  PK: string;
  SK: string;
  subscribed_at?: string;
}

/**
 * Return the set of park keys the current user is subscribed to.
 *
 * Used by the /me page to render the toggle state. Implemented as a
 * Scan with filter at this scale — 4 parks × N users is small enough
 * that adding a GSI for "all subscriptions for one user" isn't worth
 * the cost. If user count grows past ~hundreds, add a GSI on USER#<sub>
 * and switch to Query.
 */
export async function getUserParkSubscriptions(
  sub: string,
): Promise<Set<ParkKey>> {
  const resp = await client.send(
    new ScanCommand({
      TableName: tableName,
      FilterExpression: "SK = :sk AND begins_with(PK, :pk)",
      ExpressionAttributeValues: { ":sk": `USER#${sub}`, ":pk": "PARK#" },
    }),
  );
  const out = new Set<ParkKey>();
  for (const row of (resp.Items ?? []) as ParkSubRow[]) {
    const parkKey = row.PK.replace(/^PARK#/, "") as ParkKey;
    out.add(parkKey);
  }
  return out;
}

// ─── Favorite rides (M3 Phase 2) ─────────────────────────────────────
//
// Schema: USER#<sub> / FAV_RIDE#<ride_id> with denormalized park_key.
//
// The denormalized park_key on each row trades 12 bytes per favorite
// for two query benefits:
//   1. /me/rides/[park] can fetch a user's favorites for ONE park
//      with a Query + FilterExpression instead of fetching all
//      favorites and filtering client-side.
//   2. A future "all favorites grouped by park" view groups locally
//      without joining against the RIDE# table.
//
// The poller's "who favorited ride X?" lookup is a different access
// pattern — it queries by ride_id, not by user — and will need a
// GSI on FAV_RIDE#<ride_id> when Phase 2's poller change lands.
// We don't add the GSI in the data layer because the GSI projection
// is sized to the poller's needs, not this module's.

interface FavRideRow {
  PK: string;
  SK: string;
  park_key?: ParkKey;
  ride_name?: string;
  favorited_at?: string;
}

/**
 * Return the set of ride_ids the user has favorited in `parkKey`.
 *
 * Query (not Scan) on PK=USER#<sub>, SK begins_with FAV_RIDE#, then
 * FilterExpression on park_key — DDB charges for the items the
 * filter evaluates, not just the matches, but at <100 favorites
 * per user the cost is rounding error.
 */
export async function getUserFavoriteRides(
  sub: string,
  parkKey: ParkKey,
): Promise<Set<string>> {
  const resp = await client.send(
    new QueryCommand({
      TableName: tableName,
      KeyConditionExpression: "PK = :pk AND begins_with(SK, :skp)",
      FilterExpression: "park_key = :park",
      ExpressionAttributeValues: {
        ":pk": `USER#${sub}`,
        ":skp": "FAV_RIDE#",
        ":park": parkKey,
      },
    }),
  );
  const out = new Set<string>();
  for (const row of (resp.Items ?? []) as FavRideRow[]) {
    out.add(row.SK.replace(/^FAV_RIDE#/, ""));
  }
  return out;
}

/**
 * Toggle a favorite-ride row on or off.
 *
 * Subscribed=true: PutItem with denormalized park_key + ride_name +
 * favorited_at. Both extra attributes are best-effort metadata for
 * future views; the poller only cares that the row exists.
 *
 * Subscribed=false: DeleteItem.
 *
 * Idempotent — repeat calls produce the same end state. The poller's
 * fanout (Phase 2) picks up the change on its next 2-min tick.
 */
export async function setFavoriteRide(
  sub: string,
  rideId: string,
  parkKey: ParkKey,
  rideName: string,
  isFavorite: boolean,
): Promise<void> {
  const Key = { PK: `USER#${sub}`, SK: `FAV_RIDE#${rideId}` };
  if (isFavorite) {
    await client.send(
      new PutCommand({
        TableName: tableName,
        Item: {
          ...Key,
          park_key: parkKey,
          ride_name: rideName,
          favorited_at: new Date().toISOString(),
        },
      }),
    );
  } else {
    await client.send(new DeleteCommand({ TableName: tableName, Key }));
  }
}
