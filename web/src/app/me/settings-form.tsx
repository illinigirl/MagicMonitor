"use client";

/**
 * Client-side wrapper for the /me settings form.
 *
 * Lives in a small client component (instead of being inline in
 * page.tsx) for two reasons:
 *   1. useActionState is a client hook — needed to surface the
 *      success/error message after the server action runs.
 *   2. The submit button needs useFormStatus to disable itself
 *      mid-flight; useFormStatus only reads from the nearest <form>
 *      ancestor in a client tree.
 *
 * The page (server component) hands us the initial values it read
 * from DDB. We never re-fetch on the client — re-render is driven
 * by revalidatePath() inside the server action.
 *
 * Layout: the poster design's two-column grid — PROFILE (inputs +
 * save) left, PARKS TO WATCH (checkbox rows) right; both live in one
 * <form> so a single save covers profile + subscriptions, unchanged.
 */

import Link from "next/link";
import { useActionState, useState, useTransition } from "react";
import { useFormStatus } from "react-dom";

import { PARKS, type ParkKey } from "@/lib/parks";
import {
  saveSettings,
  sendTestNotification,
  type SaveSettingsResult,
  type TestNotifResult,
} from "./actions";

interface Props {
  initialName: string;
  initialPushoverUserKey: string;
  initialSubscribedParks: ParkKey[];
  /** Server-fetched count of favorited rides per park, for inline display. */
  favoriteCountsByPark: Record<ParkKey, number>;
}

/** Oswald section heading with the 2px teal bottom rule. */
function ColumnHead({ children }: { children: React.ReactNode }) {
  return (
    <legend className="head w-full border-b-2 border-line pb-2 text-lg">
      {children}
    </legend>
  );
}

export function SettingsForm({
  initialName,
  initialPushoverUserKey,
  initialSubscribedParks,
  favoriteCountsByPark,
}: Props) {
  const [state, formAction] = useActionState<SaveSettingsResult | null, FormData>(
    saveSettings,
    null,
  );

  const subscribedSet = new Set(initialSubscribedParks);

  return (
    <form action={formAction}>
      <div className="grid grid-cols-1 gap-9 md:grid-cols-2">
        <fieldset>
          <ColumnHead>Profile</ColumnHead>

          <div className="mt-5">
            <Field label="Display name" htmlFor="name">
              <input
                id="name"
                name="name"
                type="text"
                required
                defaultValue={initialName}
                autoComplete="name"
                className="retro-input"
              />
            </Field>
          </div>

          <div className="mt-5">
            <Field
              label="Pushover user key"
              htmlFor="pushoverUserKey"
              hint="Where ride-down alerts get delivered. Find it in your Pushover dashboard."
            >
              <input
                id="pushoverUserKey"
                name="pushoverUserKey"
                type="text"
                required
                inputMode="text"
                defaultValue={initialPushoverUserKey}
                spellCheck={false}
                // monospace so character-level typos in the key are visible
                className="retro-input font-mono !text-sm"
              />
            </Field>
          </div>

          <div className="mt-5">
            <TestNotificationButton
              hasSavedKey={Boolean(initialPushoverUserKey)}
            />
          </div>

          <div className="mt-6 flex items-center gap-4">
            <SubmitButton />
            <StatusMessage state={state} />
          </div>
        </fieldset>

        <fieldset>
          <ColumnHead>Parks to watch</ColumnHead>
          <p className="mt-3 text-[13px] leading-relaxed text-fg-2">
            Pushover alerts only fire for parks you check here. You can
            change this any time — the next 2-min poll picks it up.
          </p>
          <div className="mt-4 flex flex-col gap-3">
            {PARKS.map((park) => (
              <div
                key={park.key}
                className="flex items-center gap-3.5 rounded-md border-2 border-line bg-bg-1 px-4 py-3"
              >
                {/* Label wraps only checkbox + code + name so the row's
                    toggle target doesn't include the favorites link. */}
                <label className="flex flex-1 cursor-pointer items-center gap-3.5">
                  <input
                    type="checkbox"
                    name="parks"
                    value={park.key}
                    defaultChecked={subscribedSet.has(park.key)}
                    className="retro-checkbox"
                  />
                  <span
                    className="display w-10 shrink-0 text-[15px]"
                    style={{ color: `var(${park.accentVar})` }}
                    aria-hidden
                  >
                    {park.shortName}
                  </span>
                  <span className="font-head font-semibold text-[15px] uppercase tracking-[0.06em] text-fg-0">
                    {park.name}
                  </span>
                </label>
                <Link
                  href={`/me/rides/${park.key}`}
                  className="poster-link whitespace-nowrap !text-[11px] text-accent hover:underline"
                >
                  Pick rides
                  {favoriteCountsByPark[park.key] > 0 &&
                    ` (${favoriteCountsByPark[park.key]})`}
                  {" →"}
                </Link>
              </div>
            ))}
          </div>
        </fieldset>
      </div>
    </form>
  );
}

/**
 * "Send test notification" — a type="button" (NOT a submit) that fires
 * the sendTestNotification action out-of-band, so it doesn't submit or
 * validate the settings form. Tests the SAVED key; disabled until one
 * exists (hint tells the user to Save a freshly-typed key first).
 */
function TestNotificationButton({ hasSavedKey }: { hasSavedKey: boolean }) {
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<TestNotifResult | null>(null);

  return (
    <div>
      <button
        type="button"
        disabled={pending || !hasSavedKey}
        onClick={() => {
          setResult(null);
          startTransition(async () => setResult(await sendTestNotification()));
        }}
        className="poster-link rounded-[5px] border-2 border-line bg-bg-1 px-3 py-2 text-fg-0 transition-colors duration-100 hover:border-accent hover:text-accent disabled:opacity-50"
      >
        {pending ? "Sending…" : "Send test notification"}
      </button>
      {!hasSavedKey ? (
        <p className="mt-1.5 text-xs text-fg-3">
          Save a Pushover key first, then send a test to confirm it works.
        </p>
      ) : result ? (
        result.ok ? (
          <p className="mt-1.5 text-xs text-ok">Sent — check your phone. 📲</p>
        ) : (
          <p className="mt-1.5 text-xs text-bad">{result.error}</p>
        )
      ) : (
        <p className="mt-1.5 text-xs text-fg-3">
          Sends a push to your saved key right now.
        </p>
      )}
    </div>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={htmlFor}
        className="mb-2 block font-head font-semibold text-xs uppercase tracking-[0.16em] text-fg-3"
      >
        {label}
      </label>
      {children}
      {hint && (
        <p className="mt-[7px] text-xs leading-relaxed text-fg-3">{hint}</p>
      )}
    </div>
  );
}

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-[5px] bg-accent px-7 py-3 font-head font-semibold text-sm uppercase tracking-[0.18em] text-bg-0 transition-opacity duration-100 hover:opacity-90 disabled:opacity-60"
    >
      {pending ? "Saving…" : "Save settings"}
    </button>
  );
}

function StatusMessage({ state }: { state: SaveSettingsResult | null }) {
  if (!state) return null;
  if (state.ok) {
    return (
      <span className="text-sm text-ok">
        Saved {new Date(state.savedAt).toLocaleTimeString()}.
      </span>
    );
  }
  return <span className="text-sm text-bad">{state.error}</span>;
}
