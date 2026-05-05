import clsx from "clsx";
import type { RideState } from "@/lib/dynamodb";

/**
 * One row in a ride list. Visual emphasis goes to: ride name (primary),
 * wait time or status badge (secondary), Lightning Lane info if any
 * (tertiary). DOWN gets a red badge; OPERATING shows the wait number.
 *
 * `isFavorite` (M3 Phase 2): when true, prepends a small gold star.
 * Intentionally subtle — doesn't reorder rows or change emphasis,
 * just gives signed-in users a quick scan-cue for "this one's mine."
 */
export function RideRow({
  ride,
  isFavorite = false,
}: {
  ride: RideState;
  isFavorite?: boolean;
}) {
  return (
    <li className="grid grid-cols-[1fr_auto] items-center gap-4 border-b border-line-soft py-3 last:border-b-0">
      <div className="min-w-0 flex items-center gap-2">
        {isFavorite && (
          <span
            className="text-gold text-sm shrink-0"
            aria-label="Favorited"
            title="One of your favorites"
          >
            ★
          </span>
        )}
        <div className="min-w-0">
          <p className="text-fg-0 truncate">{ride.name}</p>
          {ride.ll ? (
            <p className="label-meta mt-0.5">
              {ride.ll.type === "paid" ? `LL ${ride.ll.price ?? "$"}` : "LL Free"}
              {ride.ll.return_start && ` · ${formatTime(ride.ll.return_start)}`}
            </p>
          ) : null}
        </div>
      </div>
      <StatusBadge status={ride.status} wait={ride.wait_mins} />
    </li>
  );
}

function StatusBadge({
  status,
  wait,
}: {
  status: RideState["status"];
  wait: RideState["wait_mins"];
}) {
  if (status === "OPERATING") {
    if (wait == null) {
      return (
        <span className="label-meta text-fg-2">No wait reported</span>
      );
    }
    return (
      <span className="flex items-baseline gap-1.5">
        <span className="display text-xl font-medium text-fg-0">{wait}</span>
        <span className="label-meta">min</span>
      </span>
    );
  }
  return (
    <span
      className={clsx(
        "rounded-sm px-2 py-1 text-xs font-medium tracking-wide uppercase",
        status === "DOWN" && "bg-bad/15 text-bad",
        status === "CLOSED" && "bg-bg-3 text-fg-2",
        status === "REFURBISHMENT" && "bg-warn/15 text-warn",
      )}
    >
      {status === "REFURBISHMENT" ? "Refurb" : status}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    const dt = new Date(iso);
    return dt.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
      timeZone: "America/New_York",
    });
  } catch {
    return "";
  }
}
