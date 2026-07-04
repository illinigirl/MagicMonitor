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
  QueryCommand,
} from "@aws-sdk/lib-dynamodb";

import { PARKS, type ParkKey } from "./parks";

// Same region/table conventions as dynamodb.ts. AWS_REGION is
// unreliable on Amplify SSR (see comment in dynamodb.ts), so we pin
// to us-east-2 where the table lives.
const region = process.env.DISNEY_REGION ?? "us-east-2";
const tableName = process.env.DISNEY_TABLE_NAME ?? "DisneyData";

// Reuse the same global singleton key as the reader so dev hot-reload
// doesn't multiply the socket pool. The global is only assigned when
// NODE_ENV !== "production" (below), so in PRODUCTION this module and
// dynamodb.ts each construct their own client — two small socket pools,
// which is fine. The global cache only collapses them to one in dev.
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

// ─── Widget feed secret (2026-07-03) ─────────────────────────────────
//
// The iOS widget can't carry a NextAuth session, so the JSON feed
// (/api/widget/waits) authenticates with a per-user CAPABILITY TOKEN:
// `<sub>.<secret>`, where the secret lives on the user's PROFILE row.
// Deliberate, documented tradeoff: anyone holding the URL can read that
// user's ride names + waits (low sensitivity, no writes). Revoke by
// deleting the widget_secret attribute (a fresh one mints on next visit
// to /waits).

/** Get the user's widget secret, creating one on first use. Handles the
 *  concurrent-first-call race via if_not_exists — both callers converge
 *  on whichever secret landed. */
export async function getOrCreateWidgetSecret(sub: string): Promise<string> {
  const { randomBytes } = await import("crypto");
  const fresh = randomBytes(16).toString("hex");
  const resp = await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
      UpdateExpression: "SET widget_secret = if_not_exists(widget_secret, :s)",
      ExpressionAttributeValues: { ":s": fresh },
      ReturnValues: "ALL_NEW",
    }),
  );
  return (resp.Attributes?.widget_secret as string) ?? fresh;
}

/** The stored secret (null when never provisioned) — for feed verification. */
export async function getWidgetSecret(sub: string): Promise<string | null> {
  const resp = await client.send(
    new GetCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
    }),
  );
  return (resp.Item?.widget_secret as string | undefined) ?? null;
}

// ─── Plan-widget secret (2026-07-04) ─────────────────────────────────
//
// A SEPARATE capability token for the today's-plan widget feed. The
// waits token above is mintable by ANY signed-in user (their own data);
// the plan feed returns SHARED FAMILY data, so its token is minted only
// on /trips — behind the family email allowlist — and possession proves
// the holder passed that gate once. Distinct attribute so a non-family
// waits token can never read the plan. Same revocation story: delete
// plan_widget_secret; a fresh one mints on the next /trips visit.

/** Get the user's plan-widget secret, creating one on first use. ONLY
 *  call from behind the family gate (/trips). */
export async function getOrCreatePlanWidgetSecret(sub: string): Promise<string> {
  const { randomBytes } = await import("crypto");
  const fresh = randomBytes(16).toString("hex");
  const resp = await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
      UpdateExpression:
        "SET plan_widget_secret = if_not_exists(plan_widget_secret, :s)",
      ExpressionAttributeValues: { ":s": fresh },
      ReturnValues: "UPDATED_NEW",
    }),
  );
  return (resp.Attributes?.plan_widget_secret as string) ?? fresh;
}

/** The stored plan-widget secret (null when never provisioned). */
export async function getPlanWidgetSecret(sub: string): Promise<string | null> {
  const resp = await client.send(
    new GetCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: "PROFILE" },
    }),
  );
  return (resp.Item?.plan_widget_secret as string | undefined) ?? null;
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

/**
 * Return the set of park keys the current user is subscribed to.
 *
 * Used by the /me page to render the toggle state — and by
 * saveSettings to diff against, so a wrong answer here silently
 * breaks unsubscription, not just the display.
 *
 * One GetItem per park, in parallel: the full key
 * `PARK#<key>/USER#<sub>` is known, so this is O(parks) reads and
 * structurally independent of table size. The previous
 * implementation was a single-page Scan + FilterExpression — the
 * same shape as the 2026-05-24 getParkRides regression — which
 * silently returns nothing once the table outgrows one ~1MB scan
 * page (the table is multi-GB now; see TESTING.md "Silent
 * regressions from data growth").
 */
export async function getUserParkSubscriptions(
  sub: string,
): Promise<Set<ParkKey>> {
  const found = await Promise.all(
    PARKS.map(async (park) => {
      const resp = await client.send(
        new GetCommand({
          TableName: tableName,
          Key: { PK: `PARK#${park.key}`, SK: `USER#${sub}` },
        }),
      );
      return resp.Item ? park.key : null;
    }),
  );
  return new Set(found.filter((k): k is ParkKey => k !== null));
}

// ─── Plan alert opt-in (2026-07-03) ──────────────────────────────────
//
// ⚠️ BOUNDARY NOTE: this is the web's ONLY write into the SHARED trip
// partition (USER#megan) — everywhere else the web writes strictly
// per-user rows. Deliberately narrow: an UpdateItem that touches exactly
// one attribute (alert_subscribers) via ATOMIC set ADD/DELETE, so it can
// never race with (or clobber) the MCP planner's edits to the same rows.
// The IAM LeadingKeys grant (USER#*) already covers it — no CDK change.

/** Shared trip partition owner — must match SHARED_TRIP_USER in
 *  dynamodb.ts and the MCP planner's _SHARED_USER_ID. */
const SHARED_TRIP_USER = "megan";

/**
 * Opt the signed-in member in/out of a set of plan days' alerts.
 *
 * `sub` MUST come from the session (auth().user.id) — never the request
 * body (same rule as every write in this module). Adds/removes it in each
 * row's alert_subscribers String Set; the poller then includes the
 * member's USER#<sub>/PROFILE Pushover key in that plan's fanout. DDB
 * semantics: ADD creates the set, DELETE of the last member removes the
 * attribute (= back to owner-only).
 */
export async function setPlanAlertSubscription(
  sub: string,
  planIds: string[],
  subscribed: boolean,
): Promise<void> {
  await Promise.all(
    planIds.map((planId) =>
      client.send(
        new UpdateCommand({
          TableName: tableName,
          Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
          UpdateExpression: subscribed
            ? "ADD alert_subscribers :m"
            : "DELETE alert_subscribers :m",
          ExpressionAttributeValues: { ":m": new Set([sub]) },
          // Only mutate rows the planner actually wrote — never create.
          ConditionExpression: "attribute_exists(PK)",
        }),
      ),
    ),
  );
}

// ─── Re-plan: drop/keep a ride (2026-07-03) ──────────────────────────
//
// The /replan approve action moves a disrupted ride out of the poller's
// watch set. Like setPlanAlertSubscription, it's an ATOMIC set ADD/DELETE
// on the shared plan row (dropped_ride_ids) — NOT a read-modify-write of
// the ride_sequence list, so it can't race with or clobber a concurrent
// MCP plan edit. The poller filters dropped_ride_ids out of its active-
// plan index; ride_sequence itself is left intact so the MCP planner's
// view is unchanged and a "keep" (DELETE) cleanly restores watching.

/**
 * Drop a ride from the poller's watch set for a shared plan (dropped=
 * true), or un-drop it (false), via atomic ADD/DELETE on
 * dropped_ride_ids. Never mutates ride_sequence, so it can't race with an
 * MCP plan edit.
 */
export async function setRideDropped(
  planId: string,
  rideId: string,
  dropped: boolean,
): Promise<void> {
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      UpdateExpression: dropped
        ? "ADD dropped_ride_ids :r"
        : "DELETE dropped_ride_ids :r",
      ExpressionAttributeValues: { ":r": new Set([rideId]) },
      ConditionExpression: "attribute_exists(PK)",
    }),
  );
}

/**
 * Mark a ride done (done=true) or un-done (false) for a shared plan, via
 * atomic ADD/DELETE on completed_ride_ids. Like dropped_ride_ids: no
 * ride_sequence surgery, so it can't race a plan edit. The poller stops
 * watching it and the page shows it done. (Claude's mark_ride_complete is
 * the richer path — it also captures actual wait for calibration — but a
 * one-tap "I rode it" from the phone doesn't need that.)
 */
export async function setRideDone(
  planId: string,
  rideId: string,
  done: boolean,
): Promise<void> {
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      UpdateExpression: done
        ? "ADD completed_ride_ids :r"
        : "DELETE completed_ride_ids :r",
      ExpressionAttributeValues: { ":r": new Set([rideId]) },
      ConditionExpression: "attribute_exists(PK)",
    }),
  );
}

/**
 * Mark a ride as "do next" for a shared plan (rideId), or clear it
 * (null). A single scalar next_up set atomically (SET/REMOVE) — no
 * ride_sequence surgery, so it can't race with an MCP edit. Web /trips,
 * /replan, and the MCP planner all present next_up first so everyone
 * shares "what's next." Setting a new ride replaces the prior next_up.
 *
 * next_up_since (2026-07-03) rides along on every SET: the ISO moment
 * this ride became next up. It's the anchor for the poller's future
 * "probably off the ride — done? / grab this LL next" nudge (PICKUP #3):
 * next_up_since + expected wait + ride duration ≈ when to ask.
 */
export async function setPlanNextUp(
  planId: string,
  rideId: string | null,
): Promise<void> {
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      UpdateExpression: rideId
        ? "SET next_up = :r, next_up_since = :ts"
        : "REMOVE next_up, next_up_since",
      ...(rideId
        ? {
            ExpressionAttributeValues: {
              ":r": rideId,
              ":ts": new Date().toISOString(),
            },
          }
        : {}),
      ConditionExpression: "attribute_exists(PK)",
    }),
  );
}

// ─── One-tap "✓ Done" capability token (2026-07-03) ──────────────────
//
// The /done page marks a plan ride complete from a Pushover alert tap
// with NO Cognito session — the token in the URL is the auth, exactly
// the widget-feed pattern above (and it sidesteps the Pushover in-app
// browser's separate cookie jar entirely). Per-PLAN token stored on the
// plan row itself so the poller — which already reads plan rows every
// poll — can mint alert links later without any extra secret channel.
// Documented tradeoff: anyone holding a link can mark rides done on
// that one plan (family-only distribution, day-scoped rows, undoable).
// Revoke by deleting the done_token attribute.

/** Get the plan's done-link token, creating one on first use. Same
 *  if_not_exists convergence as getOrCreateWidgetSecret; never creates
 *  the plan row itself. Returns null when the plan doesn't exist. */
export async function getOrCreatePlanDoneToken(
  planId: string,
): Promise<string | null> {
  const { randomBytes } = await import("crypto");
  const fresh = randomBytes(16).toString("hex");
  try {
    const resp = await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
        UpdateExpression: "SET done_token = if_not_exists(done_token, :s)",
        ExpressionAttributeValues: { ":s": fresh },
        ConditionExpression: "attribute_exists(PK)",
        ReturnValues: "UPDATED_NEW",
      }),
    );
    return (resp.Attributes?.done_token as string) ?? fresh;
  } catch {
    return null; // plan row missing (condition failed) — nothing to mint
  }
}

/** The stored done-link token (null when never provisioned) — for /done
 *  verification. Projection-only read; never mints. */
export async function getPlanDoneToken(
  planId: string,
): Promise<string | null> {
  const resp = await client.send(
    new GetCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      ProjectionExpression: "done_token",
    }),
  );
  return (resp.Item?.done_token as string | undefined) ?? null;
}

/**
 * Set (isoOrNull) or clear a held Lightning Lane return time for a ride
 * on a plan, in the ll_holds map. Atomic per-key map update — ll_holds is
 * ensured first so the nested SET can't fail on a missing map; never
 * touches ride_sequence. Mirrors the MCP apply_held_ll so web + Claude
 * agree. isoOrNull=null clears the hold.
 */
export async function setHeldLl(
  planId: string,
  rideId: string,
  isoOrNull: string | null,
): Promise<void> {
  const Key = { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` };
  if (isoOrNull) {
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "SET ll_holds = if_not_exists(ll_holds, :empty)",
        ExpressionAttributeValues: { ":empty": {} },
        ConditionExpression: "attribute_exists(PK)",
      }),
    );
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "SET ll_holds.#r = :t",
        ExpressionAttributeNames: { "#r": rideId },
        ExpressionAttributeValues: { ":t": isoOrNull },
      }),
    );
  } else {
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "REMOVE ll_holds.#r",
        ExpressionAttributeNames: { "#r": rideId },
      }),
    );
  }
}

/**
 * Record (minutes) or clear (null) the ACTUAL wait for a ride, in the
 * actual_waits map — the calibration signal (predicted vs actual). Atomic
 * per-key map update, ll_holds-style; ensured first so the nested SET
 * can't fail on a missing map. Paired with Mark done on the schedule.
 */
export async function setRideActualWait(
  planId: string,
  rideId: string,
  minutes: number | null,
): Promise<void> {
  const Key = { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` };
  if (minutes !== null) {
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "SET actual_waits = if_not_exists(actual_waits, :empty)",
        ExpressionAttributeValues: { ":empty": {} },
        ConditionExpression: "attribute_exists(PK)",
      }),
    );
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "SET actual_waits.#r = :m",
        ExpressionAttributeNames: { "#r": rideId },
        ExpressionAttributeValues: { ":m": minutes },
      }),
    );
  } else {
    await client.send(
      new UpdateCommand({
        TableName: tableName,
        Key,
        UpdateExpression: "REMOVE actual_waits.#r",
        ExpressionAttributeNames: { "#r": rideId },
      }),
    );
  }
}

// ─── "Ask Claude" daily rate limit (2026-07-03) ──────────────────────
//
// The /replan "Ask Claude" server action costs real Anthropic tokens, so
// it's capped per user per day. Atomic ADD on a dated counter row (TTL'd
// to auto-expire) — returns the post-increment count so the caller can
// reject once over the cap. Row: USER#<sub>/REPLAN_LLM#<yyyy-mm-dd>.

/** Increment + return today's Ask-Claude call count for a user. */
export async function bumpReplanLlmCount(
  sub: string,
  dayIso: string,
): Promise<number> {
  const resp = await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${sub}`, SK: `REPLAN_LLM#${dayIso}` },
      UpdateExpression:
        "SET #ttl = if_not_exists(#ttl, :ttl) ADD #c :one",
      ExpressionAttributeNames: { "#ttl": "ttl", "#c": "count" },
      ExpressionAttributeValues: {
        ":one": 1,
        // ~2 days out; the row only needs to survive the current day.
        ":ttl": Math.floor(Date.now() / 1000) + 172800,
      },
      ReturnValues: "UPDATED_NEW",
    }),
  );
  return Number(resp.Attributes?.count ?? 1);
}

/**
 * Append new rides to a plan's ride_sequence (from an Ask-Claude add).
 * Uses DDB's atomic `list_append` in the UpdateExpression — a single
 * server-side append, so no read-modify-write and no race with a
 * concurrent plan edit. Idempotency (not re-adding an existing ride) is
 * the caller's job via the catalog filter.
 */
export async function addRidesToSequence(
  planId: string,
  rides: { ride_id: string; ride_name: string }[],
): Promise<void> {
  if (rides.length === 0) return;
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      UpdateExpression:
        "SET ride_sequence = list_append(if_not_exists(ride_sequence, :empty), :new)",
      ExpressionAttributeValues: {
        ":empty": [],
        ":new": rides.map((r) => ({
          ride_id: r.ride_id,
          ride_name: r.ride_name,
          added_via: "replan",
        })),
      },
      ConditionExpression: "attribute_exists(PK)",
    }),
  );
}

/**
 * Set the suggested ride ORDER for a plan (a list of ride_ids), from the
 * "Ask Claude" re-plan. A single atomic SET of one list attribute
 * (plan_order) — overwrites wholesale, never touches ride_sequence, so it
 * can't corrupt the planner's list or race a concurrent edit. Reads
 * present rides in plan_order when present; a dropped ride just won't
 * appear even if still listed.
 */
export async function setPlanOrder(
  planId: string,
  order: string[],
): Promise<void> {
  await client.send(
    new UpdateCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
      UpdateExpression: "SET plan_order = :o",
      ExpressionAttributeValues: { ":o": order },
      ConditionExpression: "attribute_exists(PK)",
    }),
  );
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
 * Per-park favorite counts for the signed-in user.
 *
 * One Query against PK=USER#<sub>, SK begins_with FAV_RIDE#, project
 * the denormalized park_key and bucket client-side. At <100 favorites
 * per user this is cheaper than 4 parallel per-park Queries and
 * gives /me both the per-park count (for "Pick favorites (N) →"
 * inline counts) and the "has any favorites" boolean for the setup
 * banner from a single round-trip.
 */
export async function getFavoriteRideCountsByPark(
  sub: string,
): Promise<Record<ParkKey, number>> {
  const resp = await client.send(
    new QueryCommand({
      TableName: tableName,
      KeyConditionExpression: "PK = :pk AND begins_with(SK, :skp)",
      ExpressionAttributeValues: {
        ":pk": `USER#${sub}`,
        ":skp": "FAV_RIDE#",
      },
      ProjectionExpression: "park_key",
    }),
  );
  const counts: Record<ParkKey, number> = {
    magic_kingdom: 0,
    epcot: 0,
    hollywood_studios: 0,
    animal_kingdom: 0,
  };
  for (const row of (resp.Items ?? []) as { park_key?: ParkKey }[]) {
    if (row.park_key && row.park_key in counts) {
      counts[row.park_key] += 1;
    }
  }
  return counts;
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
