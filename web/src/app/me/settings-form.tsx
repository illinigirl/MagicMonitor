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
 */

import Link from "next/link";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { PARKS, type ParkKey } from "@/lib/parks";
import { saveSettings, type SaveSettingsResult } from "./actions";

interface Props {
  initialName: string;
  initialPushoverUserKey: string;
  initialSubscribedParks: ParkKey[];
}

export function SettingsForm({
  initialName,
  initialPushoverUserKey,
  initialSubscribedParks,
}: Props) {
  const [state, formAction] = useActionState<SaveSettingsResult | null, FormData>(
    saveSettings,
    null,
  );

  const subscribedSet = new Set(initialSubscribedParks);

  return (
    <form action={formAction} className="space-y-8">
      <fieldset className="space-y-4">
        <legend className="display text-lg font-medium text-fg-1">
          Profile
        </legend>

        <Field label="Display name" htmlFor="name">
          <input
            id="name"
            name="name"
            type="text"
            required
            defaultValue={initialName}
            autoComplete="name"
            className="w-full rounded-md border border-line bg-bg-0 px-3 py-2 text-fg-0 focus:border-accent focus:outline-none"
          />
        </Field>

        <Field
          label="Pushover user key"
          htmlFor="pushoverUserKey"
          hint="30-character alphanumeric key from your Pushover account."
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
            className="w-full rounded-md border border-line bg-bg-0 px-3 py-2 font-mono text-sm text-fg-0 focus:border-accent focus:outline-none"
          />
        </Field>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className="display text-lg font-medium text-fg-1">
          Parks to alert me about
        </legend>
        <p className="text-fg-2 text-sm">
          Pushover alerts only fire for parks you check here. You can change
          this any time — the next 2-min poll picks it up.
        </p>
        <div className="grid grid-cols-1 gap-2">
          {PARKS.map((park) => (
            <div
              key={park.key}
              className="flex items-center gap-3 rounded-md border border-line bg-bg-1 px-3 py-2 hover:bg-bg-2"
            >
              {/* Label wraps only checkbox + name so the row's
                  toggle target doesn't include the favorites link. */}
              <label className="flex items-center gap-3 flex-1 cursor-pointer">
                <input
                  type="checkbox"
                  name="parks"
                  value={park.key}
                  defaultChecked={subscribedSet.has(park.key)}
                  className="h-4 w-4 accent-gold"
                />
                <span className="text-fg-0">{park.name}</span>
              </label>
              <Link
                href={`/me/rides/${park.key}`}
                className="text-fg-3 hover:text-fg-1 text-xs transition-colors whitespace-nowrap"
              >
                Pick favorites →
              </Link>
            </div>
          ))}
        </div>
      </fieldset>

      <div className="flex items-center gap-4">
        <SubmitButton />
        <StatusMessage state={state} />
      </div>
    </form>
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
        className="block text-sm font-medium text-fg-1 mb-1"
      >
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-fg-3">{hint}</p>}
    </div>
  );
}

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="inline-flex items-center gap-2 rounded-md bg-gold px-4 py-2 text-sm font-medium text-gold-ink hover:opacity-90 disabled:opacity-60 transition-opacity"
    >
      {pending ? "Saving…" : "Save settings"}
    </button>
  );
}

function StatusMessage({ state }: { state: SaveSettingsResult | null }) {
  if (!state) return null;
  if (state.ok) {
    return (
      <span className="text-sm text-fg-2">
        Saved {new Date(state.savedAt).toLocaleTimeString()}.
      </span>
    );
  }
  return <span className="text-sm text-bad">{state.error}</span>;
}
