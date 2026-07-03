"""
Next-up nudge — pure decision logic (M10, 2026-07-03).

The "probably off the ride?" moment: next_up_since (stamped when a ride
becomes the plan's next_up) + how long the ride plausibly takes ≈ when
to send ONE combined push: "mark it ✓ done — and here's the next
Lightning Lane worth grabbing."

Design split (Megan, 2026-07-03): these RULES decide when to speak and
which LL to surface — deterministic, free, every poll. The push's ✓-Done
tap-through lands on /done, and the full re-plan brain (Ask Claude) is
one more tap from there. LLM only on tap; zero standing token cost.

Everything here is pure: the caller injects `now` (the forecast_signal
pattern) so tests never touch the wall clock.
"""

import os
from datetime import datetime, timedelta

# Minutes added on top of the wait estimate for riding + walking off.
RIDE_BUFFER_MINS = int(os.environ.get("NUDGE_RIDE_BUFFER_MINS", "20"))
# Wait estimate for a held-LL next_up (you skip standby).
LL_WAIT_EST_MINS = int(os.environ.get("NUDGE_LL_WAIT_EST_MINS", "15"))
# Wait estimate when the plan carries no numeric prediction.
DEFAULT_WAIT_MINS = int(os.environ.get("NUDGE_DEFAULT_WAIT_MINS", "30"))
# Don't nudge on a next_up older than this — a stale pointer means the
# family moved on; a late "did you finish?" is noise, not help.
MAX_AGE_MINS = int(os.environ.get("NUDGE_MAX_AGE_MINS", "180"))


def _parse_iso(iso: str | None) -> datetime | None:
    """Aware datetime from ISO, else None. Naive timestamps are treated
    as unparseable — comparing them against an aware `now` raises, and
    a wrong-but-plausible nudge time is worse than no nudge."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else None


def _est_wait_mins(predicted_wait_min: int | None, held_ll: bool) -> int:
    if held_ll:
        return LL_WAIT_EST_MINS
    if predicted_wait_min is not None:
        return int(predicted_wait_min)
    return DEFAULT_WAIT_MINS


def nudge_fires_at(
    next_up_since_iso: str | None,
    predicted_wait_min: int | None,
    held_ll: bool,
) -> datetime | None:
    """The instant the nudge becomes due: next_up_since + estimated wait
    (LL estimate when held, else the plan's prediction) + ride buffer.
    None when there's no valid anchor timestamp."""
    since = _parse_iso(next_up_since_iso)
    if since is None:
        return None
    est = _est_wait_mins(predicted_wait_min, held_ll)
    return since + timedelta(minutes=est + RIDE_BUFFER_MINS)


def should_nudge(
    next_up_since_iso: str | None,
    predicted_wait_min: int | None,
    held_ll: bool,
    now: datetime,
) -> bool:
    """True when the party has plausibly finished the next_up ride.
    Never nudges without a timestamp, and never on a stale one."""
    since = _parse_iso(next_up_since_iso)
    if since is None:
        return False
    if (now - since).total_seconds() / 60 > MAX_AGE_MINS:
        return False
    fires_at = nudge_fires_at(next_up_since_iso, predicted_wait_min, held_ll)
    return fires_at is not None and now >= fires_at


def pick_ll_candidate(
    plan_rides: list[dict],
    ll_holds: dict[str, str],
    current_lls: dict[str, dict],
    next_up_ride_id: str | None,
    now: datetime,
) -> dict | None:
    """The next Lightning Lane worth grabbing: among the plan's REMAINING
    rides (caller passes the already-filtered list), skip rides already
    held and the one you're on (next_up), then pick the earliest usable
    return window still in the future. Deterministic v1 — rides Ask
    Claude might ADD to the plan are out of scope until you tap through.

    Returns {ride_id, ride_name, return_start, price} or None.
    """
    best: dict | None = None
    best_dt: datetime | None = None
    for r in plan_rides:
        rid = r.get("ride_id")
        if not rid or rid == next_up_ride_id or rid in ll_holds:
            continue
        offer = current_lls.get(rid) or {}
        ret = _parse_iso(offer.get("return_start"))
        if ret is None or ret < now:
            continue
        if best_dt is None or ret < best_dt:
            best_dt = ret
            best = {
                "ride_id": rid,
                "ride_name": r.get("ride_name") or rid,
                "return_start": offer.get("return_start"),
                "price": offer.get("price"),
            }
    return best
