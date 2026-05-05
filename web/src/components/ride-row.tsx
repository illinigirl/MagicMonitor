import clsx from "clsx";
import type { RideState } from "@/lib/dynamodb";

/**
 * One row in a ride list. Visual emphasis goes to: ride name (primary),
 * wait time or status badge (secondary), Lightning Lane info if any
 * (tertiary). DOWN gets a red badge; OPERATING shows the wait number.
 */
export function RideRow({ ride }: { ride: RideState }) {
  return (
    <li className="grid grid-cols-[1fr_auto] items-center gap-4 border-b border-line-soft py-3 last:border-b-0">
      <div className="min-w-0">
        <p className="text-fg-0 truncate">{ride.name}</p>
        {ride.ll ? (
          <p className="label-meta mt-0.5">
            {ride.ll.type === "paid" ? `LL ${ride.ll.price ?? "$"}` : "LL Free"}
            {ride.ll.return_start && ` · ${formatTime(ride.ll.return_start)}`}
          </p>
        ) : null}
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
