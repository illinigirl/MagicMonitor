"use client";

/**
 * Per-ride re-plan controls on /replan: Drop (skip, atomic) and Do next
 * (prioritize, atomic next_up). Both states come down from the server so
 * they survive reloads and reflect MCP-side changes. The alert's `kind`
 * decides which action is emphasized (down → Drop, next → Do next).
 */

import { useState, useTransition } from "react";

import {
  applyActualWait,
  applyDone,
  applyDrop,
  applyNextUp,
  type ReplanResult,
} from "./actions";

export default function ReplanControls({
  planId,
  rideId,
  rideName,
  initiallyDropped,
  initiallyNext,
  initiallyDone,
  initialActual,
  emphasize,
}: {
  planId: string;
  rideId: string;
  rideName: string;
  initiallyDropped: boolean;
  initiallyNext: boolean;
  initiallyDone: boolean;
  initialActual: number | null;
  /** Which action to lead with for this ride. */
  emphasize: "drop" | "next";
}) {
  const [pending, startTransition] = useTransition();
  const [dropped, setDropped] = useState(initiallyDropped);
  const [isNext, setIsNext] = useState(initiallyNext);
  const [done, setDone] = useState(initiallyDone);
  const [error, setError] = useState<string | null>(null);

  const act = (fn: () => Promise<ReplanResult>, onOk: () => void) => {
    setError(null);
    startTransition(async () => {
      const res = await fn();
      if (res.ok) onOk();
      else setError(res.error ?? "Couldn't update.");
    });
  };

  const drop = () =>
    act(() => applyDrop(planId, rideId, true), () => {
      setDropped(true);
      setIsNext(false);
    });
  const undrop = () => act(() => applyDrop(planId, rideId, false), () => setDropped(false));
  const doNext = () =>
    act(() => applyNextUp(planId, rideId, true), () => {
      setIsNext(true);
      setDropped(false);
    });
  const clearNext = () => act(() => applyNextUp(planId, rideId, false), () => setIsNext(false));
  const markDone = () =>
    act(() => applyDone(planId, rideId, true), () => {
      setDone(true);
      setIsNext(false);
    });
  const unDone = () => act(() => applyDone(planId, rideId, false), () => setDone(false));

  if (done) {
    return (
      <Row>
        <span className="rounded-full bg-ok/15 px-3 py-1 text-xs font-medium text-ok">
          Done ✓
        </span>
        <ActualWait
          planId={planId}
          rideId={rideId}
          initial={initialActual}
        />
        <TextBtn onClick={unDone} disabled={pending} label="Undo" />
        <Err error={error} />
      </Row>
    );
  }

  if (dropped) {
    return (
      <Row>
        <span className="rounded-full bg-bad/15 px-3 py-1 text-xs font-medium text-bad">
          Dropped
        </span>
        <TextBtn onClick={undrop} disabled={pending} label="Undo" />
        <Err error={error} />
      </Row>
    );
  }

  const nextBtn = isNext ? (
    <Row>
      <span className="rounded-full bg-ok/15 px-3 py-1 text-xs font-medium text-ok">
        Next up ✓
      </span>
      <TextBtn onClick={clearNext} disabled={pending} label="Clear" />
    </Row>
  ) : (
    <button
      type="button"
      onClick={doNext}
      disabled={pending}
      className={
        "rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 " +
        (emphasize === "next"
          ? "bg-gold text-gold-ink hover:opacity-90"
          : "border border-line bg-bg-1 text-fg-1 hover:bg-bg-2")
      }
    >
      {pending ? "…" : "Do next"}
    </button>
  );

  const dropBtn = (
    <button
      type="button"
      onClick={drop}
      disabled={pending}
      className={
        "rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 " +
        (emphasize === "drop"
          ? "border border-bad/40 bg-bad/10 text-bad hover:bg-bad/20"
          : "border border-line bg-bg-1 text-fg-2 hover:bg-bg-2 hover:text-fg-1")
      }
    >
      Drop
    </button>
  );

  const doneBtn = (
    <button
      type="button"
      onClick={markDone}
      disabled={pending}
      className="rounded-md border border-line bg-bg-1 px-3 py-1.5 text-sm font-medium text-fg-2 hover:bg-bg-2 hover:text-ok disabled:opacity-50"
    >
      Done
    </button>
  );

  // Lead with the emphasized action; Done is always available.
  return (
    <Row>
      {emphasize === "next" ? (
        <>
          {nextBtn}
          {!isNext && dropBtn}
        </>
      ) : (
        <>
          {dropBtn}
          {nextBtn}
        </>
      )}
      {doneBtn}
      <Err error={error} />
    </Row>
  );
}

/** Optional "actual wait" capture on a done ride — calibration data.
 *  Blank is fine; saves on blur. */
function ActualWait({
  planId,
  rideId,
  initial,
}: {
  planId: string;
  rideId: string;
  initial: number | null;
}) {
  const [pending, start] = useTransition();
  const [saved, setSaved] = useState(initial !== null);
  const [err, setErr] = useState<string | null>(null);

  const save = (v: string) => {
    setErr(null);
    start(async () => {
      const res = await applyActualWait(planId, rideId, v);
      if (res.ok) setSaved(v.trim() !== "");
      else setErr(res.error ?? "");
    });
  };
  return (
    <span className="inline-flex items-center gap-1 text-xs text-fg-3">
      actual
      <input
        type="number"
        min={0}
        max={600}
        inputMode="numeric"
        defaultValue={initial ?? ""}
        placeholder="—"
        disabled={pending}
        onBlur={(e) => save(e.target.value)}
        className="w-12 rounded border border-line bg-bg-0 px-1 py-0.5 text-center text-fg-0"
      />
      min
      {saved && <span className="text-ok">✓</span>}
      {err && <span className="text-warn">{err}</span>}
    </span>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2">{children}</div>;
}
function TextBtn({ onClick, disabled, label }: { onClick: () => void; disabled: boolean; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="text-xs text-fg-3 underline hover:text-fg-1 disabled:opacity-50"
    >
      {label}
    </button>
  );
}
function Err({ error }: { error: string | null }) {
  return error ? <span className="text-xs text-warn">{error}</span> : null;
}
