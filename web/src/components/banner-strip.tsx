"use client";

import { usePathname } from "next/navigation";

/**
 * Full-width teal banner strip directly under the masthead — part of
 * the poster chrome on every page. Copy varies by route (design
 * handoff: home/live pages get the marquee line; analytics, trips and
 * settings each get their own strip).
 *
 * Client component: the copy is pathname-driven and the masthead
 * around it is a server component, so this small leaf reads
 * usePathname() rather than threading the route through props.
 */
export function BannerStrip() {
  const pathname = usePathname() ?? "/";

  let copy = "WALT DISNEY WORLD · LIVE WAIT TIMES · UPDATED EVERY 2 MINUTES";
  if (pathname.includes("/analytics")) {
    copy = "HISTORICAL WAIT-TIME ANALYTICS";
  } else if (pathname.startsWith("/trips")) {
    copy = "THE SHARED FAMILY PLAN";
  } else if (pathname.startsWith("/me")) {
    copy = "ALERTS · PARKS · PROFILE";
  } else if (pathname.startsWith("/replan") || pathname.startsWith("/waits")) {
    copy = "TODAY AT THE PARK · LIVE";
  }

  return (
    <div
      className="bg-line text-center text-bg-0 py-[7px] font-head font-medium text-xs"
      style={{ letterSpacing: "0.34em" }}
    >
      {copy}
    </div>
  );
}
