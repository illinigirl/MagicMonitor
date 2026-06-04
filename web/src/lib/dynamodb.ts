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
import { DynamoDBDocumentClient, QueryCommand, type QueryCommandOutput } from "@aws-sdk/lib-dynamodb";

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
 * Query all current STATE rows for one park via the
 * `park_key-SK-index` GSI.
 *
 * This used to be a paginated Scan + FilterExpression that walked
 * the entire ~5 GB table to find ~25 STATE rows per park (~$0.03
 * per page load). The 2026-05-24 silent regression — single-page
 * Scan started returning 0 matches once WAIT# rows pushed STATE
 * rows past page 1 — forced the immediate pagination fix. This is
 * the category-level fix: a Query against an index that knows
 * about park_key. ~25 items returned in one round-trip,
 * ~$0.0001 per page load, structurally independent of total
 * table size.
 *
 * The GSI was added in the M6-B-Phase-4 follow-up CDK deploy
 * 2026-05-25. partitionKey=park_key, sortKey=SK, full projection.
 * STATE rows match SK="STATE" exactly; the same GSI also enables
 * SK begins_with "WAIT#" / "HIST#" Queries for future analytics
 * read paths that need to walk a park's observations.
 *
 * Pagination is still required as defense — STATE rows total ~25
 * per park and the GSI partition stays well under 1MB at current
 * scale, but the LastEvaluatedKey loop guards against future
 * growth (per the same data-shape-assumption rule that motivated
 * this fix in the first place).
 */
export async function getParkRides(parkKey: ParkKey): Promise<RideState[]> {
  const items: RideState[] = [];
  let exclusiveStartKey: Record<string, unknown> | undefined = undefined;
  do {
    const resp: QueryCommandOutput = await client.send(
      new QueryCommand({
        TableName: tableName,
        IndexName: "park_key-SK-index",
        KeyConditionExpression: "park_key = :p AND SK = :sk",
        ExpressionAttributeValues: { ":sk": "STATE", ":p": parkKey },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...((resp.Items ?? []) as RideState[]));
    exclusiveStartKey = resp.LastEvaluatedKey;
  } while (exclusiveStartKey);
  return items.sort((a, b) => a.name.localeCompare(b.name));
}

// ─── Multi-day trips (shared family planner) ────────────────────────
//
// The MCP trip planner writes to ONE shared partition (USER#megan) — not
// per-user; identity is attribution only (created_by). So the dashboard
// reads trips from this fixed partition, and the /trips page gates access
// to the family at the page layer (NOT per logged-in sub, unlike /me).

const SHARED_TRIP_USER = "megan";

export interface TripDay {
  date: string;
  park_key: ParkKey;
  plan_id: string;
  active: boolean;
  ride_count: number;
  outcome_recorded: boolean;
  rides: { ride_name: string; ride_id?: string }[];
}

export interface Trip {
  trip_id: string;
  name: string | null;
  start_date: string;
  end_date: string;
  days: TripDay[];
}

interface PlanRow {
  SK: string;
  trip_id?: string;
  planned_for_date?: string;
  park_key?: ParkKey;
  active?: boolean;
  outcome_recorded?: boolean;
  ride_sequence?: { ride_name?: string; ride_id?: string }[];
}

interface TripRow {
  SK: string;
  name?: string;
}

/** Today's date as YYYY-MM-DD in Eastern (the parks' tz), matching the
 *  planner's planned_for_date convention. */
function todayEtIso(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

/** Paginated Query of USER#<SHARED_TRIP_USER> rows whose SK begins_with
 *  the given prefix. Paginate defensively — the partition is small today
 *  but never single-page a partition that grows (the getParkRides lesson). */
async function querySharedTripRows<T>(skPrefix: string): Promise<T[]> {
  const items: T[] = [];
  let exclusiveStartKey: Record<string, unknown> | undefined = undefined;
  do {
    const resp: QueryCommandOutput = await client.send(
      new QueryCommand({
        TableName: tableName,
        KeyConditionExpression: "PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues: {
          ":pk": `USER#${SHARED_TRIP_USER}`,
          ":sk": skPrefix,
        },
        ExclusiveStartKey: exclusiveStartKey,
      }),
    );
    items.push(...((resp.Items ?? []) as T[]));
    exclusiveStartKey = resp.LastEvaluatedKey;
  } while (exclusiveStartKey);
  return items;
}

/**
 * Upcoming (or in-progress) shared family trips, soonest first. Each
 * trip's days + date range are DERIVED from its PLAN# rows, not the
 * TRIP# header's denormalized `days` (which can drift) — mirrors the
 * MCP get_upcoming_trip (Y) model. A trip is "upcoming" if its latest
 * day is today-or-later (ET). Trips with no day rows are skipped.
 */
export async function getUpcomingTrips(): Promise<Trip[]> {
  const today = todayEtIso();
  const [tripRows, planRows] = await Promise.all([
    querySharedTripRows<TripRow>("TRIP#"),
    querySharedTripRows<PlanRow>("PLAN#"),
  ]);

  const byTrip = new Map<string, PlanRow[]>();
  for (const p of planRows) {
    if (!p.trip_id) continue;
    const arr = byTrip.get(p.trip_id);
    if (arr) arr.push(p);
    else byTrip.set(p.trip_id, [p]);
  }

  const trips: Trip[] = [];
  for (const hdr of tripRows) {
    const tripId = hdr.SK.slice("TRIP#".length);
    const allRows = (byTrip.get(tripId) ?? []).filter((r) => r.planned_for_date);
    if (allRows.length === 0) continue;
    // Collapse to one row per date (prefer the active plan, else the
    // most-recently-recorded SK) — defensive against duplicate day rows,
    // so a date never renders twice even if a dup slipped in.
    const byDate = new Map<string, PlanRow>();
    for (const r of allRows) {
      const d = r.planned_for_date!;
      const cur = byDate.get(d);
      const better =
        !cur ||
        (Number(Boolean(r.active)) > Number(Boolean(cur.active))) ||
        (Boolean(r.active) === Boolean(cur.active) && r.SK > cur.SK);
      if (better) byDate.set(d, r);
    }
    const rows = [...byDate.values()].sort((a, b) =>
      a.planned_for_date! < b.planned_for_date! ? -1 : 1,
    );
    const endDate = rows[rows.length - 1].planned_for_date!;
    if (endDate < today) continue; // already over
    trips.push({
      trip_id: tripId,
      name: hdr.name ?? null,
      start_date: rows[0].planned_for_date!,
      end_date: endDate,
      days: rows.map((r) => ({
        date: r.planned_for_date!,
        park_key: (r.park_key ?? "magic_kingdom") as ParkKey,
        plan_id: r.SK.slice("PLAN#".length),
        active: Boolean(r.active),
        ride_count: (r.ride_sequence ?? []).length,
        outcome_recorded: Boolean(r.outcome_recorded),
        rides: (r.ride_sequence ?? []).map((rd) => ({
          ride_name: rd.ride_name ?? "(unnamed)",
          ride_id: rd.ride_id,
        })),
      })),
    });
  }
  trips.sort((a, b) => (a.start_date < b.start_date ? -1 : 1));
  return trips;
}
