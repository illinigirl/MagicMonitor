/**
 * Server actions for the /me settings page.
 *
 * One action handles the whole form: profile fields + park toggles
 * in a single submit. The single-form UX is simpler than a per-toggle
 * approach (works without JS, no flicker, atomic-feeling save) and
 * cheap on the server side because we batch the writes.
 *
 * Auth contract: every action calls `auth()` and uses the resulting
 * Cognito sub for ALL DDB partition-key construction. The form's
 * client never names a user — it just sends "the current user's"
 * settings. Cross-user write attempts are impossible at this layer
 * because there is no client-controlled user identifier.
 */
"use server";

import { revalidatePath } from "next/cache";

import { auth } from "@/auth";
import {
  getUserParkSubscriptions,
  getUserProfile,
  putUserProfile,
  setParkSubscription,
} from "@/lib/dynamodb-writes";
import { PARKS, findPark, type ParkKey } from "@/lib/parks";
import {
  sendPushoverMessage,
  validatePushoverUserKey,
} from "@/lib/pushover";

const PARK_KEYS = new Set<ParkKey>(PARKS.map((p) => p.key));

export type SaveSettingsResult =
  | { ok: true; savedAt: string }
  | { ok: false; error: string };

export async function saveSettings(
  _prevState: SaveSettingsResult | null,
  formData: FormData,
): Promise<SaveSettingsResult> {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) {
    return { ok: false, error: "Not signed in." };
  }

  const name = (formData.get("name") ?? "").toString().trim();
  const pushoverUserKey = (formData.get("pushoverUserKey") ?? "")
    .toString()
    .trim();

  if (!name) {
    return { ok: false, error: "Display name is required." };
  }
  if (!pushoverUserKey) {
    return { ok: false, error: "Pushover user key is required." };
  }

  // Park toggles arrive as FormData entries: parks=magic_kingdom,
  // parks=epcot, etc. Filter to known keys so a hand-crafted POST
  // can't write a PARK#<arbitrary>/USER#<sub> row.
  const requestedParks = new Set<ParkKey>(
    formData
      .getAll("parks")
      .map((v) => v.toString())
      .filter((v): v is ParkKey => PARK_KEYS.has(v as ParkKey)),
  );

  // Validate the Pushover key only when it changed. Keeps the
  // round-trip out of the common "I'm just toggling parks" flow.
  const existingProfile = await getUserProfile(sub);
  if (
    !existingProfile ||
    existingProfile.pushoverUserKey !== pushoverUserKey
  ) {
    const result = await validatePushoverUserKey(pushoverUserKey);
    if (!result.valid) {
      return { ok: false, error: `Pushover: ${result.reason}` };
    }
  }

  // Diff the current subscriptions vs the desired set so we only
  // write what's actually changing (cheaper, quieter audit log).
  const currentParks = await getUserParkSubscriptions(sub);
  const toAdd: ParkKey[] = [];
  const toRemove: ParkKey[] = [];
  for (const park of PARK_KEYS) {
    const wanted = requestedParks.has(park);
    const have = currentParks.has(park);
    if (wanted && !have) toAdd.push(park);
    if (!wanted && have) toRemove.push(park);
  }

  // Profile first (single UpdateItem). Then subscriptions in
  // parallel — independent partition keys, no ordering constraint.
  await putUserProfile(sub, { name, pushoverUserKey });
  await Promise.all([
    ...toAdd.map((p) => setParkSubscription(sub, p, true)),
    ...toRemove.map((p) => setParkSubscription(sub, p, false)),
  ]);

  // Confirmation push when subscriptions changed. Skipped when only
  // the profile changed (saving a Pushover key shouldn't spam) or
  // when nothing changed at all (re-saves are no-ops).
  if (toAdd.length > 0 || toRemove.length > 0) {
    try {
      await sendPushoverMessage(
        pushoverUserKey,
        buildSubscriptionChangeBody(requestedParks),
        {
          title: "Magic Monitor — alerts updated",
          url: settingsUrl(),
          urlTitle: "Change settings",
        },
      );
    } catch (err) {
      // The save itself succeeded; a failed confirmation push is
      // a soft error — log so we notice patterns, but return ok.
      console.warn("[me/save] confirmation push failed:", err);
    }
  }

  // Re-read the page on next render so the form reflects what's
  // actually in the table (defends against stale local state if
  // the user has /me open in another tab).
  revalidatePath("/me");

  return { ok: true, savedAt: new Date().toISOString() };
}

export type TestNotifResult = { ok: true } | { ok: false; error: string };

// Warm-Lambda-scoped debounce for the test button — swallows rapid
// double-taps without a DDB write. Best-effort (not durable across cold
// starts or instances), which is all a debounce needs to be; the client
// also disables the button mid-flight.
const testCooldown: Map<string, number> =
  ((globalThis as { __mmTestCooldown?: Map<string, number> }).__mmTestCooldown ??=
    new Map());
const TEST_COOLDOWN_MS = 5000;

/**
 * Send a one-off Pushover "test alert" to the signed-in user's SAVED
 * key — the self-serve "is my Pushover actually wired up?" check.
 *
 * Uses the key already on the profile (not the form field), so if the
 * user just typed a new key they should Save first; the button hint says
 * so. Identity is the session sub — no client-supplied user id, same
 * contract as every other action here.
 */
export async function sendTestNotification(): Promise<TestNotifResult> {
  const session = await auth();
  const sub = session?.user?.id;
  if (!sub) return { ok: false, error: "Not signed in." };

  const profile = await getUserProfile(sub);
  if (!profile?.pushoverUserKey) {
    return {
      ok: false,
      error: "Save a Pushover user key first, then send a test.",
    };
  }

  const now = Date.now();
  if (now - (testCooldown.get(sub) ?? 0) < TEST_COOLDOWN_MS) {
    return { ok: false, error: "Just sent one — give it a few seconds." };
  }
  testCooldown.set(sub, now);

  try {
    await sendPushoverMessage(
      profile.pushoverUserKey,
      "🎢 Test alert from Magic Monitor — if you can see this, your alerts are set up correctly.",
      { title: "Magic Monitor — test", url: settingsUrl(), urlTitle: "My alerts" },
    );
  } catch (err) {
    // Surface as a soft error — most likely a stale/rotated key.
    console.warn("[me/test] test push failed:", err);
    return {
      ok: false,
      error: "Couldn't send — double-check your Pushover key and try again.",
    };
  }
  return { ok: true };
}

function buildSubscriptionChangeBody(active: Set<ParkKey>): string {
  if (active.size === 0) {
    return "You're no longer subscribed to alerts for any park. Visit Settings to opt back in.";
  }
  const names = Array.from(active)
    .map((k) => findPark(k)?.name ?? k)
    .sort();
  return `You're now getting alerts for: ${formatList(names)}.`;
}

/** "A", "A and B", "A, B, and C" — Oxford comma + the conjunction. */
function formatList(items: string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function settingsUrl(): string {
  // Prefer the Auth.js v5 canonical name; fall back to the v4 alias
  // we keep set in env for compatibility.
  const base = process.env.AUTH_URL ?? process.env.NEXTAUTH_URL ?? "";
  return base ? `${base.replace(/\/$/, "")}/me` : "/me";
}
