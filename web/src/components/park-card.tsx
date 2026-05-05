import Link from "next/link";
import clsx from "clsx";
import type { Park } from "@/lib/parks";
import {
  formatTimeShort,
  isEarlyEntryNow,
  isExtraHoursNow,
  isParkOpenNow,
  type ParkSchedule,
} from "@/lib/schedule";

/**
 * Landing-page tile for a single park. The accent strip on the left
 * uses the park's CSS variable so each card visually claims its
 * park's hue without hardcoding hex values.
 *
 * Schedule is optional — if it's missing (API hiccup) the card just
 * omits the status line rather than showing wrong info.
 */
export function ParkCard({
  park,
  schedule,
}: {
  park: Park;
  schedule: ParkSchedule | null;
}) {
  return (
    <Link
      href={`/parks/${park.key}`}
      className="group relative flex items-stretch overflow-hidden rounded-lg border border-line bg-bg-1 hover:bg-bg-2 transition-colors shadow-[var(--shadow-card)]"
      style={
        { "--park-accent": `var(${park.accentVar})` } as React.CSSProperties
      }
    >
      <div
        className="w-2 shrink-0"
        style={{ background: "var(--park-accent)" }}
      />
      <div className="flex-1 px-6 py-5">
        <div className="flex items-baseline gap-3">
          <h2 className="display text-2xl font-medium text-fg-0">{park.name}</h2>
          <span className="label-meta">{park.shortName}</span>
        </div>
        <p className="mt-2 text-fg-2 text-sm">{park.tagline}</p>
        {schedule && <CardStatusLine schedule={schedule} />}
        <p className="mt-4 text-xs text-fg-3 group-hover:text-fg-2 transition-colors">
          Live status →
        </p>
      </div>
    </Link>
  );
}

function CardStatusLine({ schedule }: { schedule: ParkSchedule }) {
  const open = isParkOpenNow(schedule);
  const earlyEntry = isEarlyEntryNow(schedule);
  const extraHours = isExtraHoursNow(schedule);

  let badge: { label: string; classes: string };
  let trail = "";

  if (open && schedule.todayOperating) {
    badge = { label: "Open", classes: "bg-ok/15 text-ok" };
    trail = `until ${formatTimeShort(schedule.todayOperating.close)}`;
  } else if (extraHours && schedule.todayExtraHours) {
    badge = { label: "Extended Evening", classes: "bg-info/15 text-info" };
    trail = `until ${formatTimeShort(schedule.todayExtraHours.close)} · deluxe / DVC`;
  } else if (earlyEntry && schedule.todayOperating) {
    badge = { label: "Early Entry", classes: "bg-info/15 text-info" };
    trail = `general at ${formatTimeShort(schedule.todayOperating.open)}`;
  } else if (schedule.tomorrowOperating) {
    badge = { label: "Closed", classes: "bg-bg-3 text-fg-2" };
    trail = `opens tomorrow ${formatTimeShort(schedule.tomorrowOperating.open)}`;
  } else {
    badge = { label: "Closed", classes: "bg-bg-3 text-fg-2" };
  }

  return (
    <p className="mt-3 flex items-center gap-2 text-sm">
      <span
        className={clsx(
          "rounded-sm px-2 py-0.5 text-[10px] font-semibold tracking-wider uppercase",
          badge.classes,
        )}
      >
        {badge.label}
      </span>
      {trail && <span className="text-fg-2">{trail}</span>}
    </p>
  );
}
