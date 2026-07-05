import Link from "next/link";

import { HeaderAuth } from "@/components/auth/HeaderAuth";
import { BannerStrip } from "@/components/banner-strip";

/**
 * Poster masthead — appears on every route inside the teal frame.
 * Logo left (Alfa Slab One, "Monitor" in red-orange), nav right
 * (Oswald caps via HeaderAuth: TRIPS / SETTINGS / SIGN OUT), a 2px
 * teal rule underneath, then the full-width teal banner strip whose
 * copy varies per page.
 */
export function SiteHeader() {
  return (
    <header>
      <div className="flex items-center justify-between gap-6 px-8 pt-5 pb-3.5 border-b-2 border-line">
        <Link href="/">
          <h1 className="display text-[26px] leading-none text-fg-0">
            Magic <span className="text-accent">Monitor</span>
          </h1>
        </Link>
        <HeaderAuth />
      </div>
      <BannerStrip />
    </header>
  );
}
