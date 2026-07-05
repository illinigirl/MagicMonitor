/**
 * Small shared poster motifs (design handoff 2026-07-05). Pure
 * presentational server components — no hooks, no data.
 */

/** Diamond ornament — two red-orange rules flanking a rotated square.
 *  Sits centered under hero titles. */
export function DiamondRule() {
  return (
    <div className="mt-3 flex items-center justify-center gap-3" aria-hidden>
      <div className="h-0.5 w-[70px] bg-accent" />
      <div className="h-2 w-2 rotate-45 bg-accent" />
      <div className="h-0.5 w-[70px] bg-accent" />
    </div>
  );
}
