"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * Masthead nav link — Oswald caps, teal; the active route gets the
 * 2px red-orange bottom border. Client component only for the
 * usePathname() active check.
 */
export function NavItem({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname() ?? "/";
  const active = pathname === href || pathname.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      className={`font-head font-semibold text-[13px] uppercase text-fg-0 hover:text-accent transition-colors duration-100 pb-0.5 ${
        active ? "border-b-2 border-accent" : ""
      }`}
      style={{ letterSpacing: "0.18em" }}
    >
      {children}
    </Link>
  );
}
