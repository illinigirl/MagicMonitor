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
import { DynamoDBDocumentClient, GetCommand, QueryCommand, type QueryCommandOutput } from "@aws-sdk/lib-dynamodb";

import { findPark, type ParkKey } from "./parks";

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

/**
 * Cognito sub of the human who owns the shared trip space (Megan). The
 * poller alerts the shared owner ("megan") implicitly on every plan, so
 * on /trips this viewer is always subscribed and never appears in a
 * plan's alert_subscribers set. Hardcoded here alongside SHARED_TRIP_USER
 * (same convention); the sub is already public in infra/cdk.json's
 * mcp_sub_user_map, so this exposes nothing new.
 */
export const SHARED_TRIP_OWNER_SUB = "e1bb9500-40d1-701b-ba48-684e500ecd1d";

/**
 * Resolve a set of Cognito subs to their friendly profile names (the
 * `name` on USER#<sub>/PROFILE). Used to render the /trips alert roster —
 * "who's getting alerts for this trip" — so MCP- or web-set subscriptions
 * are visible by name. One GetItem per sub (tiny N: the family). Unknown
 * subs fall back to a short id so the row never renders blank.
 */
export async function getMemberNames(
  subs: string[],
): Promise<Record<string, string>> {
  const unique = [...new Set(subs)].filter(Boolean);
  const entries = await Promise.all(
    unique.map(async (sub) => {
      const resp = await client.send(
        new GetCommand({ TableName: tableName, Key: { PK: `USER#${sub}`, SK: "PROFILE" } }),
      );
      const name = (resp.Item?.name as string | undefined)?.trim();
      return [sub, name || `${sub.slice(0, 6)}…`] as const;
    }),
  );
  return Object.fromEntries(entries);
}

export interface TripDay {
  date: string;
  park_key: ParkKey;
  plan_id: string;
  active: boolean;
  ride_count: number;
  outcome_recorded: boolean;
  rides: { ride_name: string; ride_id?: string }[];
  /** ADDITIONAL alert recipients opted in on this day (Cognito subs).
   *  The plan owner is implicit and never stored. Powers the /trips
   *  "get alerts" toggle state. */
  alert_subscribers: string[];
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
  // DDB String Set — the DocumentClient unmarshalls SS to a JS Set.
  alert_subscribers?: Set<string> | string[];
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
        // Set (from DDB SS) or array — normalize to a serializable array
        // (a JS Set can't cross the Server Component boundary as a prop).
        alert_subscribers: [...(r.alert_subscribers ?? [])].sort(),
      })),
    });
  }

  // Standalone single-day plans (no trip_id) — e.g. a same-day record_plan,
  // which writes a lone PLAN# row with no TRIP# header. Without this they'd be
  // invisible everywhere: they live in the shared partition (not /me), and the
  // trip loop above only renders plans attached to a TRIP# header. Surface each
  // upcoming one as a single-day entry, collapsing to one row per date (same
  // active-then-newest preference as the grouped path).
  const soloByDate = new Map<string, PlanRow>();
  for (const p of planRows) {
    if (p.trip_id) continue; // grouped plans already handled above
    if (!p.planned_for_date) continue;
    if (p.planned_for_date < today) continue; // already over
    const d = p.planned_for_date;
    const cur = soloByDate.get(d);
    const better =
      !cur ||
      Number(Boolean(p.active)) > Number(Boolean(cur.active)) ||
      (Boolean(p.active) === Boolean(cur.active) && p.SK > cur.SK);
    if (better) soloByDate.set(d, p);
  }
  for (const r of soloByDate.values()) {
    const date = r.planned_for_date!;
    const parkKey = (r.park_key ?? "magic_kingdom") as ParkKey;
    trips.push({
      trip_id: `solo:${date}`,
      // A same-day plan reads as "Today's plan"; a standalone future day
      // (rare) falls back to its park name, since "today" wouldn't be true.
      name: date === today ? "Today's plan" : (findPark(parkKey)?.name ?? null),
      start_date: date,
      end_date: date,
      days: [
        {
          date,
          park_key: parkKey,
          plan_id: r.SK.slice("PLAN#".length),
          active: Boolean(r.active),
          ride_count: (r.ride_sequence ?? []).length,
          outcome_recorded: Boolean(r.outcome_recorded),
          rides: (r.ride_sequence ?? []).map((rd) => ({
            ride_name: rd.ride_name ?? "(unnamed)",
            ride_id: rd.ride_id,
          })),
          alert_subscribers: [...(r.alert_subscribers ?? [])].sort(),
        },
      ],
    });
  }

  trips.sort((a, b) => (a.start_date < b.start_date ? -1 : 1));
  return trips;
}

// ─── Single-plan read for /replan ────────────────────────────────────

export interface ReplanContext {
  plan_id: string;
  date: string;
  park_key: ParkKey;
  park_name: string;
  active: boolean;
  outcome_recorded: boolean;
  /** Rides still in the sequence (not dropped, not completed). */
  rides: { ride_name: string; ride_id: string }[];
  /** ride_ids already dropped via the /replan approve flow. */
  dropped_ride_ids: string[];
  /** ride_id the family marked "do next" (or null). */
  next_up: string | null;
}

/**
 * Read one shared-trip plan by its id (the PLAN# SK suffix) for the
 * /replan page. Returns null if it doesn't exist. Keyed GetItem — no
 * scan. The plan lives in the shared partition (USER#megan); /replan is
 * gated to the family at the page layer, same as /trips.
 */
export async function getReplanContext(
  planId: string,
): Promise<ReplanContext | null> {
  const resp = await client.send(
    new GetCommand({
      TableName: tableName,
      Key: { PK: `USER#${SHARED_TRIP_USER}`, SK: `PLAN#${planId}` },
    }),
  );
  const r = resp.Item as
    | (PlanRow & {
        dropped_ride_ids?: Set<string> | string[];
        next_up?: string;
      })
    | undefined;
  if (!r) return null;
  return {
    plan_id: planId,
    date: r.planned_for_date ?? "",
    park_key: (r.park_key ?? "magic_kingdom") as ParkKey,
    park_name: findPark(r.park_key ?? "magic_kingdom")?.name ?? (r.park_key ?? ""),
    active: Boolean(r.active),
    outcome_recorded: Boolean(r.outcome_recorded),
    rides: (r.ride_sequence ?? [])
      .filter((rd) => rd.ride_id)
      .map((rd) => ({ ride_name: rd.ride_name ?? "(unnamed)", ride_id: rd.ride_id! })),
    dropped_ride_ids: [...(r.dropped_ride_ids ?? [])],
    next_up: r.next_up ?? null,
  };
}
