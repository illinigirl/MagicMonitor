/**
 * Server-only Pushover client.
 *
 * Two responsibilities:
 *   1. Lazy-load the Pushover app token from SSM (cached forever in
 *      module scope — token rarely rotates, ~50ms first-call cost).
 *   2. Validate a user-supplied Pushover user key by calling the
 *      Pushover validate endpoint with that token.
 *
 * The Lambda poller has its own copy of this logic in Python; the
 * decision to duplicate rather than share via a service is deliberate
 * — keeps the SSR Lambda's cold-start path independent of the poller
 * and means each module can be reasoned about on its own.
 */
import "server-only";
import {
  SSMClient,
  GetParameterCommand,
} from "@aws-sdk/client-ssm";

const region = process.env.DISNEY_REGION ?? "us-east-2";
const PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json";
const PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json";

declare global {
  // eslint-disable-next-line no-var
  var __ssmClient: SSMClient | undefined;
  // eslint-disable-next-line no-var
  var __pushoverAppToken: string | undefined;
}

const ssm =
  globalThis.__ssmClient ?? new SSMClient({ region });
if (process.env.NODE_ENV !== "production") globalThis.__ssmClient = ssm;

async function getAppToken(): Promise<string> {
  if (globalThis.__pushoverAppToken) return globalThis.__pushoverAppToken;
  const paramName = process.env.PUSHOVER_APP_TOKEN_PARAM;
  if (!paramName) {
    throw new Error(
      "PUSHOVER_APP_TOKEN_PARAM env var is unset. Check Amplify env / .env.local.",
    );
  }
  const resp = await ssm.send(
    new GetParameterCommand({ Name: paramName, WithDecryption: true }),
  );
  const token = resp.Parameter?.Value;
  if (!token) {
    throw new Error(`SSM parameter ${paramName} returned no value.`);
  }
  // Cache for the life of the warm Lambda. Cold start re-fetches.
  globalThis.__pushoverAppToken = token;
  return token;
}

export type PushoverValidationResult =
  | { valid: true }
  | { valid: false; reason: string };

/**
 * Validate a Pushover user key against the Pushover API.
 *
 * Returns `{ valid: true }` if Pushover accepts the key under our
 * app token, `{ valid: false, reason }` for malformed / inactive
 * keys. Throws only on transport/SSM failure (caller should treat
 * that as a 500, not a validation error).
 *
 * Pushover's validate endpoint returns HTTP 200 with `status: 1`
 * for valid keys, HTTP 4xx with `status: 0` and an `errors` array
 * for invalid ones.
 */
export async function validatePushoverUserKey(
  userKey: string,
): Promise<PushoverValidationResult> {
  // Quick format check before spending an HTTP round-trip — Pushover
  // user keys are 30-char alphanumeric. Catches obvious typos and
  // empty/whitespace inputs without hitting the API.
  if (!/^[A-Za-z0-9]{30}$/.test(userKey)) {
    return { valid: false, reason: "Pushover user key must be 30 alphanumeric characters." };
  }

  const token = await getAppToken();
  const body = new URLSearchParams({ token, user: userKey });

  const resp = await fetch(PUSHOVER_VALIDATE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    // Pushover is fast (<1s typical), but cap so a hung connection
    // doesn't block the user's form submit forever.
    signal: AbortSignal.timeout(5000),
  });

  // Pushover returns JSON either way (4xx or 200).
  const data = (await resp.json()) as {
    status?: number;
    errors?: string[];
  };

  if (data.status === 1) return { valid: true };
  return {
    valid: false,
    reason: data.errors?.join("; ") ?? "Pushover rejected this user key.",
  };
}

/**
 * Send a Pushover message to a single user. Used today for
 * settings-change confirmations ("you are now subscribed to X").
 *
 * Throws on transport failure or Pushover-side rejection. Callers
 * that send confirmations should wrap in try/catch — a failed
 * notification shouldn't fail the underlying save.
 */
export async function sendPushoverMessage(
  userKey: string,
  message: string,
  opts: { title?: string; url?: string; urlTitle?: string } = {},
): Promise<void> {
  const token = await getAppToken();
  const body = new URLSearchParams({ token, user: userKey, message });
  if (opts.title) body.set("title", opts.title);
  if (opts.url) body.set("url", opts.url);
  if (opts.urlTitle) body.set("url_title", opts.urlTitle);

  const resp = await fetch(PUSHOVER_MESSAGES_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    signal: AbortSignal.timeout(5000),
  });

  const data = (await resp.json()) as { status?: number; errors?: string[] };
  if (data.status !== 1) {
    throw new Error(
      `Pushover send failed: ${data.errors?.join("; ") ?? `status=${data.status}`}`,
    );
  }
}
