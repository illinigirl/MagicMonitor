/**
 * Server-only Claude client for the /replan "Ask Claude" suggestion.
 *
 * Calls Sonnet with the live plan context and forces a structured
 * response (a single tool) so we get clean JSON — either "no changes" or
 * a short list of proposed changes. The API key is loaded from SSM at
 * runtime (same pattern as pushover.ts); only the param NAME is in env.
 *
 * Cost/abuse posture: this is only ever reached from a TAP on /replan,
 * behind the family gate + a per-user daily cap (see the server action).
 * A single call is a few cents at Sonnet.
 */
import "server-only";

import Anthropic from "@anthropic-ai/sdk";
import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";

const region = process.env.DISNEY_REGION ?? "us-east-2";
const MODEL = "claude-sonnet-4-6";

declare global {
  // eslint-disable-next-line no-var
  var __ssmClientReplan: SSMClient | undefined;
  // eslint-disable-next-line no-var
  var __anthropicKey: string | undefined;
}

const ssm = globalThis.__ssmClientReplan ?? new SSMClient({ region });
if (process.env.NODE_ENV !== "production") globalThis.__ssmClientReplan = ssm;

async function getKey(): Promise<string> {
  if (globalThis.__anthropicKey) return globalThis.__anthropicKey;
  const name = process.env.ANTHROPIC_API_KEY_PARAM;
  if (!name) throw new Error("ANTHROPIC_API_KEY_PARAM unset.");
  const resp = await ssm.send(
    new GetParameterCommand({ Name: name, WithDecryption: true }),
  );
  const key = resp.Parameter?.Value;
  if (!key) throw new Error(`SSM ${name} returned no value.`);
  globalThis.__anthropicKey = key;
  return key;
}

export interface ReplanRideInput {
  ride_id: string;
  ride_name: string;
  predicted_wait_min: number | null;
  current_wait: number | null;
  status: string;
  held_ll: string | null; // ISO, if the party holds an LL for it
}

/** The slice of ReplanContext this module needs (kept structural so the
 *  pure builder is testable without the DDB layer). */
export interface ReplanPlanSlice {
  rides: { ride_id: string; ride_name: string; predicted_wait_min: number | null }[];
  dropped_ride_ids: string[];
  completed_ride_ids: string[];
  held_lls: Record<string, string>;
}

export interface LiveRideSlice {
  ride_id: string;
  name: string;
  wait_mins: number | null;
  status: string;
}

/**
 * Build the model's view of the day from the plan + live state. Pure —
 * this is the boundary where 2026-07-03's bug lived: completed rides
 * leaked into "remaining planned rides", so Sonnet re-planned a
 * fictional day (6 already-ridden rides presented as still pending) and
 * its narration couldn't match what applying could ever change.
 *
 * - rides: remaining only — NOT dropped and NOT completed.
 * - completed_names: what's already ridden, passed as context (pacing,
 *   "worth adding more?") — never as re-plannable rides.
 * - catalog: park rides not in the plan at all (any status but CLOSED;
 *   DOWN stays visible so Claude knows it exists but won't pick it).
 */
export function buildReplanModelInput(
  plan: ReplanPlanSlice,
  live: LiveRideSlice[],
): {
  rides: ReplanRideInput[];
  completed_names: string[];
  catalog: { ride_id: string; ride_name: string; current_wait: number | null; status: string }[];
} {
  const byId = new Map(live.map((r) => [r.ride_id, r]));
  const done = new Set(plan.completed_ride_ids);
  const gone = new Set([...plan.dropped_ride_ids, ...plan.completed_ride_ids]);
  const rides = plan.rides
    .filter((r) => !gone.has(r.ride_id))
    .map((r) => {
      const l = byId.get(r.ride_id);
      return {
        ride_id: r.ride_id,
        ride_name: r.ride_name,
        predicted_wait_min: r.predicted_wait_min,
        current_wait: l?.wait_mins ?? null,
        status: l?.status ?? "UNKNOWN",
        held_ll: plan.held_lls[r.ride_id] ?? null,
      };
    });
  const completed_names = plan.rides
    .filter((r) => done.has(r.ride_id))
    .map((r) => r.ride_name);
  const planned = new Set(plan.rides.map((r) => r.ride_id));
  const catalog = live
    .filter((r) => !planned.has(r.ride_id) && r.status !== "CLOSED")
    .map((r) => ({
      ride_id: r.ride_id,
      ride_name: r.name,
      current_wait: r.wait_mins,
      status: r.status,
    }));
  return { rides, completed_names, catalog };
}

export interface ReplanSuggestion {
  /** True when the current order is already good — order/drop echo it. */
  no_change: boolean;
  summary: string;
  /** Remaining rides in the suggested order (ride_ids), best next first. */
  order: string[];
  /** ride_ids to drop entirely (down / not worth it). */
  drop: string[];
  /** New rides to ADD (from the park catalog): ride_id + name. */
  add: { ride_id: string; ride_name: string }[];
  /** Optional short note per ride_id explaining a move/drop/add. */
  reasons: Record<string, string>;
}

const TOOL = {
  name: "propose_replan",
  description:
    "Re-evaluate the remaining plan: return the suggested ORDER of the " +
    "remaining rides (best next first), any to DROP, and any to ADD from " +
    "the provided catalog. Use exact ride_ids from the plan or catalog " +
    "only — never invent one. Anything you mention adding or dropping in " +
    "`summary` MUST also appear in `add`/`drop`, or it won't happen.",
  input_schema: {
    type: "object" as const,
    properties: {
      no_change: {
        type: "boolean",
        description:
          "True if the current order is already good (still return it in `order`).",
      },
      summary: {
        type: "string",
        description:
          "One or two plain sentences the family reads at a glance — the gist of the re-plan.",
      },
      order: {
        type: "array",
        items: { type: "string" },
        description:
          "ALL remaining (non-dropped) ride_ids, in the suggested order, best next first.",
      },
      drop: {
        type: "array",
        items: { type: "string" },
        description: "ride_ids to drop (down or not worth the time).",
      },
      add: {
        type: "array",
        description:
          "New rides to add — ONLY ride_ids from the provided catalog. Empty unless the family asked or it clearly helps.",
        items: {
          type: "object",
          properties: {
            ride_id: { type: "string" },
            ride_name: { type: "string" },
          },
          required: ["ride_id", "ride_name"],
        },
      },
      reasons: {
        type: "object",
        description:
          "Optional map of ride_id → short reason for a move or drop.",
        additionalProperties: { type: "string" },
      },
    },
    required: ["no_change", "summary", "order", "drop"],
  },
};

export async function proposeReplan(input: {
  park_name: string;
  date: string;
  weather: string | null;
  trigger: string | null;
  /** Free-text context the family typed (e.g. "leaving by 5, skip water rides"). */
  note: string | null;
  rides: ReplanRideInput[];
  /** Names of rides already ridden today — context only, never re-planned. */
  completed_names?: string[];
  /** Other rides in the park (not in the plan) Claude may add from. */
  catalog: { ride_id: string; ride_name: string; current_wait: number | null; status: string }[];
}): Promise<ReplanSuggestion> {
  const client = new Anthropic({ apiKey: await getKey() });

  const rideLines = input.rides
    .map((r) => {
      const bits = [
        `${r.ride_name}`,
        `now ${r.status === "DOWN" ? "DOWN" : r.current_wait ?? "?"}`,
        r.predicted_wait_min != null ? `planned ~${r.predicted_wait_min}m` : null,
        r.held_ll ? `HELD LL (ignore standby)` : null,
      ].filter(Boolean);
      return `- ${bits.join(", ")}`;
    })
    .join("\n");

  const msg = await client.messages.create({
    model: MODEL,
    max_tokens: 1024,
    tools: [TOOL],
    tool_choice: { type: "tool", name: "propose_replan" },
    system:
      "You re-evaluate a family's Walt Disney World ride plan in real time, " +
      "the way you would if they pasted the alert into a chat. Re-sequence the " +
      "REMAINING rides so the best next ride is first — favor rides that are " +
      "unusually short now or that a coming storm threatens (do indoor first). " +
      "A ride marked HELD LL means they hold a Lightning Lane for it — IGNORE " +
      "its standby wait; keep it where it fits their LL time, don't reorder " +
      "around the standby. Drop rides that are DOWN or clearly not worth the " +
      "time. You may ADD rides — but ONLY ride_ids from the provided catalog, " +
      "and only when the family asked or it clearly helps (e.g. time to spare). " +
      "Never invent a ride_id. Put every non-dropped ride_id (planned + added) " +
      "in `order`. If nothing needs changing, set no_change=true but still " +
      "return the order. Keep summary + reasons short.",
    messages: [
      {
        role: "user",
        content:
          `Park: ${input.park_name} (${input.date}). ` +
          `Weather: ${input.weather ?? "n/a"}. ` +
          `Alert that prompted this: ${input.trigger ?? "manual check"}.\n\n` +
          (input.note ? `From the family: "${input.note}". Weigh this heavily.\n\n` : "") +
          (input.completed_names?.length
            ? `Already ridden today (context only — do NOT put these in order/drop): ` +
              `${input.completed_names.join(", ")}.\n\n`
            : "") +
          (input.rides.length
            ? `Remaining planned rides (current wait vs planned):\n${rideLines}\n\n`
            : `NO planned rides remain — everything is already ridden or ` +
              `dropped. Suggest ADDs from the catalog if the day has time ` +
              `left, or say the day looks complete.\n\n`) +
          (input.catalog.length
            ? `Other rides in the park you may ADD (use these exact ride_ids only):\n` +
              input.catalog
                .map((c) => `- ${c.ride_name} [${c.ride_id}] now ${c.status === "DOWN" ? "DOWN" : c.current_wait ?? "?"}`)
                .join("\n") + "\n\n"
            : "") +
          `Re-sequence, drop, and/or add as needed. Only add when the family asked or it clearly helps.`,
      },
    ],
  });

  const block = msg.content.find((b) => b.type === "tool_use");
  if (!block || block.type !== "tool_use") {
    return { no_change: true, summary: "No suggestion available right now.", order: [], drop: [], add: [], reasons: {} };
  }
  const out = block.input as ReplanSuggestion;
  const catalogById = new Map(input.catalog.map((c) => [c.ride_id, c.ride_name]));
  // Adds must be real catalog ride_ids not already planned.
  const planned = new Set(input.rides.map((r) => r.ride_id));
  const rawAdd = out.add ?? [];
  const add = rawAdd
    .filter((a) => catalogById.has(a.ride_id) && !planned.has(a.ride_id))
    .map((a) => ({ ride_id: a.ride_id, ride_name: catalogById.get(a.ride_id) ?? a.ride_name }));
  // Boundary log (2026-07-03): what the model SAID vs what validation
  // kept. Without this, a narrated-but-filtered add ("I'll add Dumbo!")
  // is indistinguishable from the model never proposing one — the exact
  // ambiguity that made today's bug hard to pin down.
  const eaten = rawAdd.filter((a) => !add.some((k) => k.ride_id === a.ride_id));
  console.log(
    `[replan/ask] sonnet returned order=${(out.order ?? []).length} ` +
      `drop=${(out.drop ?? []).length} add=${rawAdd.length}; kept add=${add.length}` +
      (eaten.length
        ? ` — FILTERED adds (bad/duplicate ride_id): ${eaten
            .map((a) => `${a.ride_name ?? "?"}[${a.ride_id}]`)
            .join(", ")}`
        : ""),
  );
  const addSet = new Set(add.map((a) => a.ride_id));
  // Valid ride universe = planned + adds.
  const ids = new Set([...planned, ...addSet]);
  const drop = (out.drop ?? []).filter((id) => ids.has(id));
  const dropSet = new Set(drop);
  // Only real, non-dropped ride_ids; append any the model forgot so the
  // order always covers every remaining (and added) ride.
  const order = (out.order ?? []).filter((id) => ids.has(id) && !dropSet.has(id));
  for (const id of ids) {
    if (!dropSet.has(id) && !order.includes(id)) order.push(id);
  }
  return {
    no_change: Boolean(out.no_change),
    summary: out.summary ?? "",
    order,
    drop,
    add,
    reasons: out.reasons ?? {},
  };
}
