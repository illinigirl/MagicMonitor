import clsx from "clsx";
import {
  formatTimeShort,
  isEarlyEntryNow,
  isExtraHoursNow,
  isParkOpenNow,
  type ParkSchedule,
} from "@/lib/schedule";

/**
 * Park-status header strip. Three pieces of info, in priority order:
 *
 *   1. Big OPEN/CLOSED badge so you know at a glance
 *   2. The next-most-relevant time (when it closes / when it opens)
 *   3. Today's full hours strip (regular + Early Entry + EEH if any)
 *
 * Designed so a glance at the top of the page answers "is it open right
 * now and should I drive over." Detail comes from the strip below.
 */
export function ParkSchedule({ schedule }: { schedule: ParkSchedule | null }) {
  if (!schedule) {
    return (
      <p className="label-meta mt-4">Hours unavailable</p>
    );
  }

  const open = isParkOpenNow(schedule);
  const earlyEntry = isEarlyEntryNow(schedule);
  const extraHours = isExtraHoursNow(schedule);

  return (
    <div className="mt-5 space-y-3">
      <div className="flex items-baseline gap-3 flex-wrap">
        <StatusBadge
          open={open}
          earlyEntry={earlyEntry}
          extraHours={extraHours}
        />
        <NextTimeLine
          schedule={schedule}
          open={open}
          earlyEntry={earlyEntry}
          extraHours={extraHours}
        />
      </div>
      <HoursStrip schedule={schedule} />
    </div>
  );
}

function StatusBadge({
  open,
  earlyEntry,
  extraHours,
}: {
  open: boolean;
  earlyEntry: boolean;
  extraHours: boolean;
}) {
  let label = "Closed";
  let classes = "bg-bg-3 text-fg-2";

  if (open) {
    label = "Open";
    classes = "bg-ok/15 text-ok";
  } else if (earlyEntry) {
    label = "Early Entry";
    classes = "bg-info/15 text-info";
  } else if (extraHours) {
    label = "Extended Evening";
    classes = "bg-info/15 text-info";
  }

  return (
    <span
      className={clsx(
        "rounded-sm px-2.5 py-1 text-xs font-semibold tracking-wider uppercase",
        classes,
      )}
    >
      {label}
    </span>
  );
}

function NextTimeLine({
  schedule,
  open,
  earlyEntry,
  extraHours,
}: {
  schedule: ParkSchedule;
  open: boolean;
  earlyEntry: boolean;
  extraHours: boolean;
}) {
  if (open && schedule.todayOperating) {
    return (
      <span className="text-fg-2 text-sm">
        Closes at{" "}
        <span className="text-fg-0">
          {formatTimeShort(schedule.todayOperating.close)}
        </span>
        {schedule.todayExtraHours && (
          <>
            {" "}· Extended Evening{" "}
            <span className="text-fg-0">
              {formatTimeShort(schedule.todayExtraHours.open)}–
              {formatTimeShort(schedule.todayExtraHours.close)}
            </span>
            <span className="label-meta ml-1.5">deluxe / DVC</span>
          </>
        )}
      </span>
    );
  }
  if (earlyEntry && schedule.todayOperating) {
    return (
      <span className="text-fg-2 text-sm">
        General opening at{" "}
        <span className="text-fg-0">
          {formatTimeShort(schedule.todayOperating.open)}
        </span>
        <span className="label-meta ml-1.5">resort guests only until then</span>
      </span>
    );
  }
  if (extraHours && schedule.todayExtraHours) {
    return (
      <span className="text-fg-2 text-sm">
        Until{" "}
        <span className="text-fg-0">
          {formatTimeShort(schedule.todayExtraHours.close)}
        </span>
        <span className="label-meta ml-1.5">deluxe / DVC only</span>
      </span>
    );
  }
  // Closed — surface the next opening.
  if (schedule.tomorrowOperating) {
    return (
      <span className="text-fg-2 text-sm">
        Opens tomorrow at{" "}
        <span className="text-fg-0">
          {formatTimeShort(schedule.tomorrowOperating.open)}
        </span>
      </span>
    );
  }
  return null;
}

function HoursStrip({ schedule }: { schedule: ParkSchedule }) {
  if (schedule.today.length === 0) return null;
  return (
    <div className="flex items-center gap-2 flex-wrap label-meta">
      <span>Today:</span>
      {schedule.today.map((seg, i) => (
        <span
          key={`${seg.kind}-${i}`}
          className={clsx(
            "px-2 py-0.5 rounded-sm",
            seg.kind === "operating" && "bg-bg-2 text-fg-1",
            seg.kind === "early_entry" && "bg-info/10 text-info",
            seg.kind === "extended_evening" && "bg-gold-soft text-gold",
          )}
          style={
            seg.kind === "extended_evening"
              ? { background: "var(--gold-soft)", color: "var(--gold)" }
              : undefined
          }
        >
          {labelFor(seg.kind)} {formatTimeShort(seg.open)}–
          {formatTimeShort(seg.close)}
        </span>
      ))}
    </div>
  );
}

function labelFor(kind: "operating" | "early_entry" | "extended_evening"): string {
  switch (kind) {
    case "operating":
      return "Park";
    case "early_entry":
      return "Early Entry";
    case "extended_evening":
      return "Extended Evening";
  }
}
