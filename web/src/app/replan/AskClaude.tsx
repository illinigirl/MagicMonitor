"use client";

/**
 * "Ask Claude" on /replan — a holistic re-plan suggestion (or "no changes
 * needed"), the same thing you'd get chatting with Claude, brought onto
 * the page. Tap → server-side Sonnet call → render its take, with an
 * Approve button per proposed change that applies it via the existing
 * atomic actions. Nothing changes until you approve.
 */

import { useState, useTransition } from "react";

import { applyReplanOrder, askClaudeReplan, type AskClaudeResult } from "./actions";
import { formatEtTime } from "@/lib/format-et";
import type { ReplanSuggestion } from "@/lib/claude-replan";

export default function AskClaude({
  planId,
  trigger,
  rideNames,
}: {
  planId: string;
  trigger?: string | null;
  /** ride_id → name, so the suggestion can label rides. */
  rideNames: Record<string, string>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<AskClaudeResult | null>(null);
  const [note, setNote] = useState("");

  const ask = () => {
    setResult(null);
    start(async () => setResult(await askClaudeReplan(planId, trigger, note)));
  };

  return (
    <div className="mb-6 rounded-lg border border-gold/40 bg-bg-1 p-4 shadow-[var(--shadow-card)]">
      <div>
        <p className="text-fg-1 text-sm font-medium">Not sure what to do?</p>
        <p className="text-fg-3 text-xs">
          Add anything Claude should know, then ask for a take on the day.
        </p>
      </div>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={2}
        maxLength={500}
        placeholder="e.g. leaving by 5, skip water rides, add a couple more headliners…"
        className="mt-2 w-full rounded-md border border-line bg-bg-0 px-3 py-2 text-sm text-fg-0 placeholder:text-fg-3 focus:border-accent focus:outline-none"
      />
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={ask}
          disabled={pending}
          className="rounded-md bg-gold px-3 py-1.5 text-sm font-medium text-gold-ink hover:opacity-90 disabled:opacity-60"
        >
          {pending ? "Thinking…" : "Ask Claude"}
        </button>
      </div>

      {result && !result.ok && (
        <p className="mt-3 text-xs text-warn">{result.error}</p>
      )}
      {result && result.ok && (
        <Suggestion planId={planId} suggestion={result.suggestion} rideNames={rideNames} />
      )}
    </div>
  );
}

function Suggestion({
  planId,
  suggestion,
  rideNames,
}: {
  planId: string;
  suggestion: ReplanSuggestion;
  rideNames: Record<string, string>;
}) {
  const [pending, start] = useTransition();
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Names for planned rides plus any Claude proposed adding.
  const addNames = Object.fromEntries(suggestion.add.map((a) => [a.ride_id, a.ride_name]));
  const name = (id: string) => rideNames[id] ?? addNames[id] ?? "(ride)";

  const apply = () => {
    setError(null);
    start(async () => {
      const res = await applyReplanOrder(
        planId,
        suggestion.order,
        suggestion.drop,
        suggestion.add,
        suggestion.times,
      );
      if (res.ok) setApplied(true);
      else setError(res.error ?? "Couldn't apply.");
    });
  };

  return (
    <div className="mt-3 border-t border-line-soft pt-3">
      <p className="text-fg-1 text-sm">{suggestion.summary}</p>

      <ol className="mt-3 space-y-1">
        {suggestion.order.map((id, i) => (
          <li key={id} className="flex items-baseline gap-2 text-sm">
            <span className="text-fg-3 text-xs w-4">{i + 1}.</span>
            {suggestion.times[id] && (
              <span className="text-fg-3 text-xs tabular-nums">
                {formatEtTime(suggestion.times[id])}
              </span>
            )}
            <span className="text-fg-0">{name(id)}</span>
            {suggestion.reasons[id] && (
              <span className="text-fg-3 text-xs">— {suggestion.reasons[id]}</span>
            )}
          </li>
        ))}
      </ol>

      {suggestion.add.length > 0 && (
        <p className="mt-2 text-xs text-ok">
          + Add: {suggestion.add.map((a) => a.ride_name).join(", ")}
        </p>
      )}
      {suggestion.drop.length > 0 && (
        <p className="mt-2 text-xs text-bad">
          Drop: {suggestion.drop.map(name).join(", ")}
        </p>
      )}

      <div className="mt-3 flex items-center gap-3">
        {applied ? (
          <span className="text-xs text-ok">Applied ✓ — this is your order now.</span>
        ) : (
          <button
            type="button"
            onClick={apply}
            disabled={pending}
            className="rounded-md bg-gold px-3 py-1.5 text-sm font-medium text-gold-ink hover:opacity-90 disabled:opacity-60"
          >
            {pending ? "Applying…" : suggestion.no_change ? "Keep this order" : "Apply this order"}
          </button>
        )}
        {error && <span className="text-xs text-warn">{error}</span>}
      </div>
    </div>
  );
}
