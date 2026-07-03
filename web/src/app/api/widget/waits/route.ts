/**
 * GET /api/widget/waits?t=<sub>.<secret> — the phone-widget JSON feed.
 *
 * A widget can't carry a NextAuth session, so this authenticates with the
 * per-user capability token minted on /waits (secret lives on the
 * PROFILE row — see dynamodb-writes.ts for the tradeoff + revocation).
 * Read-only, one user's favorites/plan waits + weather; same read model
 * as the page (getMyWaits) so widget and page can't drift.
 *
 * Comparison is constant-time over digests (timingSafeEqual requires
 * equal lengths). Failures are a uniform 401 — don't leak whether the
 * sub exists.
 */
import { createHash, timingSafeEqual } from "crypto";

import { NextRequest, NextResponse } from "next/server";

import { getWidgetSecret } from "@/lib/dynamodb-writes";
import { getMyWaits } from "@/lib/my-waits";
import { getCurrentConditions } from "@/lib/weather";

export const dynamic = "force-dynamic";

function digest(s: string): Buffer {
  return createHash("sha256").update(s).digest();
}

export async function GET(req: NextRequest) {
  const token = req.nextUrl.searchParams.get("t") ?? "";
  const dot = token.indexOf(".");
  if (dot <= 0) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const sub = token.slice(0, dot);
  const secret = token.slice(dot + 1);

  const stored = await getWidgetSecret(sub);
  if (!stored || !timingSafeEqual(digest(secret), digest(stored))) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const [waits, weather] = await Promise.all([
    getMyWaits(sub),
    getCurrentConditions(),
  ]);
  return NextResponse.json(
    { ...waits, weather },
    { headers: { "cache-control": "no-store" } },
  );
}
