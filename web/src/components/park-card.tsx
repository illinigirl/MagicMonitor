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
 * Landing-page ticket-stub card (poster design): a 64px colored stub
 * on the left carries the park code vertically, separated from the
 * card body by a dashed-cream "perforation." Body holds name, blurb,
 * status pill + hours, and the two links.
 *
 * The card can't be one big <a> — it has two destinations (live ride
 * status + today's showtimes) and HTML doesn't nest anchors. The body
 * Link is the primary click target; "Today's shows" is a sibling.
 * Hover shifts the border to red-orange (group on the wrapper).
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
    <div
      className="group flex overflow-hidden rounded-md border-2 border-line bg-bg-1 transition-colors duration-100 hover:border-accent"
      style={
        { "--park-accent": `var(${park.accentVar})` } as React.CSSProperties
      }
    >
      {/* Ticket stub: park accent fill, vertical code, dashed perforation */}
      <div
        className="flex w-16 shrink-0 items-center justify-center border-r-2 border-dashed border-bg-0"
        style={{ background: "var(--park-accent)" }}
      >
        <span
          className="display text-xl text-bg-0"
          style={{
            writingMode: "vertical-rl",
            transform: "rotate(180deg)",
            letterSpacing: "0.2em",
          }}
        >
          {park.shortName}
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-2 px-[22px] py-5">
        <Link href={`/parks/${park.key}`} className="block">
          <h2
            className="font-head font-semibold text-[22px] uppercase text-fg-0"
            style={{ letterSpacing: "0.06em" }}
          >
            {park.name}
          </h2>
          <p className="mt-1.5 text-[13.5px] leading-normal text-fg-2">
            {park.tagline}
          </p>
          {schedule && <CardStatusLine schedule={schedule} />}
        </Link>
        <div className="mt-1.5 flex flex-wrap gap-x-[18px] gap-y-1">
          <Link
            href={`/parks/${park.key}`}
            className="poster-link border-b-2 border-accent pb-0.5 text-accent"
          >
            Live ride status →
          </Link>
          <Link
            href={`/parks/${park.key}/today`}
            className="poster-link text-fg-0 hover:text-accent"
          >
            Today&apos;s shows →
          </Link>
        </div>
      </div>
    </div>
  );
}

function CardStatusLine({ schedule }: { schedule: ParkSchedule }) {
  const open = isParkOpenNow(schedule);
  const earlyEntry = isEarlyEntryNow(schedule);
  const extraHours = isExtraHoursNow(schedule);

  let badge: { label: string; classes: string };
  let trail = "";

  if (open && schedule.todayOperating) {
    badge = { label: "Open", classes: "border-ok text-ok" };
    trail = `until ${formatTimeShort(schedule.todayOperating.close)}`;
  } else if (extraHours && schedule.todayExtraHours) {
    badge = { label: "Extended Evening", classes: "border-info text-info" };
    trail = `until ${formatTimeShort(schedule.todayExtraHours.close)} · deluxe / DVC`;
  } else if (earlyEntry && schedule.todayOperating) {
    badge = { label: "Early Entry", classes: "border-info text-info" };
    trail = `general at ${formatTimeShort(schedule.todayOperating.open)}`;
  } else if (schedule.tomorrowOperating) {
    badge = { label: "Closed", classes: "border-fg-3 text-fg-3" };
    trail = `opens tomorrow ${formatTimeShort(schedule.tomorrowOperating.open)}`;
  } else {
    badge = { label: "Closed", classes: "border-fg-3 text-fg-3" };
  }

  return (
    <p className="mt-2.5 flex items-center gap-2.5">
      <span
        className={clsx(
          "rounded-full border-[1.5px] px-2.5 py-[3px] font-head font-semibold text-[11px] uppercase",
          badge.classes,
        )}
        style={{ letterSpacing: "0.14em" }}
      >
        {badge.label}
      </span>
      {trail && <span className="text-[13px] text-fg-2">{trail}</span>}
    </p>
  );
}
