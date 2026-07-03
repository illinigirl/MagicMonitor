"""
Pushover notification sender.

Reads credentials from SSM Parameter Store on cold start and caches them
for the lifetime of the Lambda container.

Pushover API docs: https://pushover.net/api

The shape mirrors the Pi version's notifier.py — same alert types
(ride down, ride up, still down, low wait) so the demo prose lines
up with the existing app.
"""

import os
import time
from datetime import datetime
from typing import Optional

import boto3
import requests

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_APP_TOKEN_PARAM = os.environ["PUSHOVER_APP_TOKEN_PARAM"]

# Park emoji prefixes for visual scanning in Pushover. Matches the Pi
# version's notifier.py so the family's existing alert format is
# preserved.
PARK_EMOJI = {
    "magic_kingdom":     "🏰",
    "epcot":             "🌐",
    "hollywood_studios": "🎬",
    "animal_kingdom":    "🐘",
}

_ssm = boto3.client("ssm")
_app_token: Optional[str] = None


def _get_app_token() -> str:
    """Lazily fetch + cache the Pushover app token from SSM."""
    global _app_token
    if _app_token is None:
        resp = _ssm.get_parameter(Name=PUSHOVER_APP_TOKEN_PARAM, WithDecryption=True)
        _app_token = resp["Parameter"]["Value"]
    return _app_token


def _send(
    user_key: str,
    title: str,
    message: str,
    priority: int = 0,
    url: str | None = None,
    url_title: str | None = None,
) -> bool:
    """POST a single Pushover message. Returns True on success.

    Priority: 0 = normal, -1 = quiet (no sound), 1 = high (bypass quiet
    hours). DOWN alerts use priority 1 so they punch through; LOW WAIT
    and back-up alerts use priority 0.

    `url`/`url_title`: Pushover's supplementary URL — a tappable deep
    link (e.g. the /replan approve page) so an alert is actionable
    without the Claude app.
    """
    try:
        # _get_app_token() is inside the try on purpose: it hits SSM lazily
        # on the first alert a container sends, and an SSM throttle/timeout
        # there must be CONTAINED to this one send (return False), not
        # raised out of the per-attraction loop — which would abort the
        # whole poll after the DOWN cooldown was already marked, losing the
        # alert for the full cooldown window on the EventBridge retry.
        payload = {
            "token":    _get_app_token(),
            "user":     user_key,
            "title":    title,
            "message":  message,
            "priority": priority,
        }
        if url:
            payload["url"] = url
            if url_title:
                payload["url_title"] = url_title
        resp = requests.post(PUSHOVER_URL, data=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notifier] alert send failed for user={user_key[:6]}…: {e}")
        return False


# ─── Public alert functions ─────────────────────────────────────────
# Each takes the recipient's Pushover user_key explicitly so the caller
# (handler) controls fanout. Returns True/False per recipient.

def alert_ride_down(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    avg_downtime_mins: Optional[int] = None,
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} is DOWN"
    body = f"{park_name}\n{ride_name} just went down."
    if avg_downtime_mins:
        body += f"\nTypical outage: ~{avg_downtime_mins} min."
    return _send(user_key, title, body, priority=1)


def alert_ride_up(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: Optional[int],
    actual_downtime_mins: Optional[int],
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} is back up"
    body = f"{park_name}\n{ride_name} is operating again."
    if actual_downtime_mins is not None:
        body += f"\nDown for {actual_downtime_mins} min."
    if wait_mins is not None:
        body += f"\nCurrent wait: {wait_mins} min."
    return _send(user_key, title, body, priority=0)


def alert_still_down(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    minutes_down: int,
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} still down"
    body = f"{park_name}\n{ride_name} has been down for {minutes_down} min."
    return _send(user_key, title, body, priority=0)


def alert_low_wait(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: int,
    typical_wait_mins: int | None = None,
    forecast_wait_mins: int | None = None,
) -> bool:
    """Fire when a ride's current wait beats one of two baselines:
    historical (typical for this hour) or today's forecast.

    Body text adapts to which baseline(s) triggered — the LOW_WAIT
    and LOW_VS_FORECAST signals share this notifier + a single
    cooldown row, so a ride gets one low-wait-class push per window
    regardless of which condition tripped. At least one of
    `typical_wait_mins` / `forecast_wait_mins` must be provided.

    Lower priority than DOWN — it's an opportunity, not a breakdown
    — but worth opening Pushover for.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} — low wait now"
    # Order the comparisons: typical first (the all-time anchor),
    # forecast second (the today-specific add-on). Both null is a
    # caller bug — render the minimal sentence and let logs surface it.
    parts: list[str] = [
        f"{park_name}",
        f"{ride_name} is at {wait_mins} min.",
    ]
    if typical_wait_mins is not None:
        parts.append(f"Typical for this hour: ~{typical_wait_mins} min.")
    if forecast_wait_mins is not None:
        parts.append(f"Today's forecast: {forecast_wait_mins} min.")
    body = "\n".join(parts[:1]) + "\n" + " ".join(parts[1:])
    return _send(user_key, title, body, priority=0)


def alert_plan_low_wait(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: int,
    typical_wait_mins: int | None = None,
    forecast_wait_mins: int | None = None,
    plan_id: Optional[str] = None,
    ride_id: Optional[str] = None,
) -> bool:
    """Plan-aware sibling of alert_low_wait: the ride with the unusually
    short wait is in the recipient's ACTIVE plan today (still in
    ride_sequence — not yet ridden), so this is directly actionable:
    jump to it now and ride it cheaper than planned.

    Same signal + cooldown as alert_low_wait (one low-wait-class push
    per ride per window); only the framing differs. Priority 0 —
    an opportunity, not a disruption.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} Plan opportunity — {ride_name} low wait"
    parts: list[str] = [
        f"{ride_name} is at {wait_mins} min right now and it's in your "
        f"plan today.",
    ]
    if typical_wait_mins is not None:
        parts.append(f"Typical for this hour: ~{typical_wait_mins} min.")
    if forecast_wait_mins is not None:
        parts.append(f"Today's forecast: {forecast_wait_mins} min.")
    parts.append("Good time to jump to it if you're close.")
    body = f"{park_name}\n" + " ".join(parts)
    url = _replan_url(plan_id, ride_id, "next")
    return _send(
        user_key, title, body, priority=0, url=url,
        url_title="Do it next or adjust" if url else None,
    )


# Public dashboard base; the /replan deep-link target. Overridable via
# env for non-prod, defaults to the live domain (already public).
_APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://magicmonitor.megillini.dev")


def _replan_url(
    plan_id: str | None,
    ride_id: str | None = None,
    kind: str | None = None,
) -> str | None:
    """Deep-link to the /replan adjust page. Needs at least a plan_id
    (any alert with plan context can link there — the human decides
    whether to act). `ride_id` highlights the alerted ride; `kind`
    (down / next / storm) tells the page which action to suggest."""
    if not plan_id:
        return None
    from urllib.parse import quote

    url = f"{_APP_BASE_URL}/replan?plan={quote(plan_id, safe='')}"
    if ride_id:
        url += f"&ride={quote(ride_id, safe='')}"
    if kind:
        url += f"&type={kind}"
    return url


def _fmt_return_time(iso: str | None) -> str | None:
    """A LL returnStart ISO ('2026-07-03T14:15:00-04:00') → '2:15 PM'.
    Returns None if unparseable so callers can degrade gracefully."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt.strftime("%-I:%M %p")


def alert_ll_earlier(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    new_return_start: str,
    prior_return_start: str | None = None,
    in_plan: bool = False,
    price: str | None = None,
    plan_id: str | None = None,
    ride_id: str | None = None,
) -> bool:
    """An earlier Lightning Lane return window just opened for a ride the
    recipient is watching (an active-plan ride, or a favorite they opted
    into). Return times usually march LATER through the day, so an earlier
    one is a genuine, time-sensitive opportunity to grab or modify a LL.

    Priority 0 — an opportunity, not a disruption. No cooldown: fires on
    each improvement (return_start earlier than the prior poll's).
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    new_t = _fmt_return_time(new_return_start) or "earlier"
    title = f"{emoji} Earlier LL — {ride_name} {new_t}"
    lead = "in your plan today" if in_plan else "on your watch list"
    parts: list[str] = [f"{ride_name}'s Lightning Lane return moved earlier"]
    prior_t = _fmt_return_time(prior_return_start)
    if prior_t:
        parts.append(f"(was {prior_t}, now {new_t})")
    else:
        parts.append(f"— now returning {new_t}")
    parts.append(f"— it's {lead}.")
    if price:
        parts.append(f"{price}.")
    parts.append("Grab it or move your existing LL earlier while it lasts.")
    body = f"{park_name}\n" + " ".join(parts)
    # In-plan recipients can act on it via /replan (do-it-next); a
    # favorites-opt-in watcher has no plan context to re-sequence.
    url = _replan_url(plan_id, ride_id, "next") if in_plan else None
    return _send(
        user_key, title, body, priority=0, url=url,
        url_title="Do it next or adjust" if url else None,
    )


def alert_plan_disruption(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    disruption_type: str,
    plan_id: Optional[str] = None,
    wait_mins: Optional[int] = None,
    ride_id: Optional[str] = None,
) -> bool:
    """Fire when a ride in the recipient's active plan for today
    transitions DOWN or BACK UP. Separate from the favoriter-based
    DOWN/UP alerts: this one fires regardless of favorites because
    being in TODAY's plan is a stronger "you care about this ride
    right now" signal than having favorited it generically.

    disruption_type:
      "went_down" — ride in plan just transitioned to DOWN.
      "back_up"   — ride in plan just came back operating.

    Priority 1 for went_down (it's actionable — replan), 0 for
    back_up (good news, less urgent).
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    # Every plan alert deep-links to /replan so it's actionable without
    # the Claude app — the human decides whether to act.
    url_title = None
    if disruption_type == "went_down":
        url = _replan_url(plan_id, ride_id, "down")
        title = f"{emoji} Plan disruption — {ride_name} DOWN"
        tail = (
            "Tap to drop it or keep it in your plan."
            if url
            else "Check with Claude — you may want to re-sequence the day."
        )
        body = (
            f"{park_name}\n"
            f"{ride_name} just went down and it's in your plan today. {tail}"
        )
        url_title = "Drop it or re-plan"
        priority = 1
    elif disruption_type == "back_up":
        url = _replan_url(plan_id, ride_id, "next")
        wait_blurb = f" Current wait: {wait_mins} min." if wait_mins is not None else ""
        title = f"{emoji} Plan update — {ride_name} back up"
        tail = "Tap to do it next or adjust." if url else "Let Claude know if you want to slot it back in."
        body = (
            f"{park_name}\n"
            f"{ride_name} is operating again and it's in your plan today."
            f"{wait_blurb} {tail}"
        )
        url_title = "Do it next or adjust"
        priority = 0
    else:
        # Defensive: unknown disruption_type. Don't crash the poller —
        # log + skip rather than throw on a typo.
        print(f"[notifier] Unknown plan disruption_type: {disruption_type!r} (ride={ride_name})")
        return False
    return _send(user_key, title, body, priority=priority, url=url, url_title=url_title)


def _done_url(plan_id: str, ride_id: str, done_token: str | None) -> str | None:
    """One-tap ✓-Done capability link (/done, sessionless — the token IS
    the auth, so it works in Pushover's in-app browser). None without a
    token; callers fall back to the /replan deep-link."""
    if not (plan_id and ride_id and done_token):
        return None
    from urllib.parse import quote

    return (
        f"{_APP_BASE_URL}/done?plan={quote(plan_id, safe='')}"
        f"&ride={quote(ride_id, safe='')}&t={quote(done_token, safe='')}"
    )


def alert_next_up_nudge(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    plan_id: str,
    ride_id: str,
    done_token: str | None = None,
    ll_ride_name: str | None = None,
    ll_return_start: str | None = None,
    ll_price: str | None = None,
) -> bool:
    """The combined "off the ride?" nudge (M10): enough time has passed
    since `ride_name` became next_up that the party has plausibly ridden
    it. One push: mark it ✓ done (tap-through = the sessionless /done
    link) and, when the rules found one, the next Lightning Lane worth
    grabbing among the plan's remaining rides. Priority 0 — a question,
    not a disruption; the per-(plan, ride) cooldown means it asks once.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} Off {ride_name}?"
    parts = [f"Probably finished {ride_name} by now — tap to mark it done."]
    if ll_ride_name and ll_return_start:
        t = _fmt_return_time(ll_return_start) or "soon"
        ll_bit = f"Next LL worth grabbing: {ll_ride_name}, returns {t}"
        if ll_price:
            ll_bit += f" ({ll_price})"
        parts.append(ll_bit + ".")
    body = f"{park_name}\n" + " ".join(parts)
    url = _done_url(plan_id, ride_id, done_token)
    url_title = f"✓ Mark {ride_name} done"
    if url is None:
        # No capability token minted yet — /replan still gets it done.
        url = _replan_url(plan_id, ride_id, "next")
        url_title = "Open today's schedule"
    return _send(
        user_key, title, body, priority=0, url=url,
        url_title=url_title if url else None,
    )


def alert_plan_drift(
    user_key: str,
    park_name: str,
    park_key: str,
    net_minutes: int,
    plan_id: Optional[str] = None,
) -> bool:
    """One gentle nudge when the remaining planned rides are collectively
    running well OFF what the plan assumed. net_minutes > 0 = LIGHTER
    (waits under prediction — time freed up, add rides); < 0 = HEAVIER
    (busier than planned, trim/re-sequence). Aggregated per plan, heavily
    cooldowned — replaces per-ride low-wait spam on a drifting day.

    Priority 0 — an opportunity/heads-up, not a disruption.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    mins = abs(int(net_minutes))
    if net_minutes >= 0:
        title = f"{emoji} Running ahead of plan"
        body = (
            f"{park_name}\n"
            f"Your remaining rides are ~{mins} min under what the plan "
            f"assumed — it's lighter than expected. Good time to add "
            f"something."
        )
        url_title = "Add a ride / adjust"
    else:
        title = f"{emoji} Busier than planned"
        body = (
            f"{park_name}\n"
            f"Your remaining rides are ~{mins} min over what the plan "
            f"assumed — heavier than expected. You may want to trim or "
            f"re-sequence."
        )
        url_title = "Trim / re-sequence"
    url = _replan_url(plan_id, None, "drift")
    return _send(
        user_key, title, body, priority=0, url=url,
        url_title=url_title if url else None,
    )


def alert_plan_weather_shift(
    user_key: str,
    park_name: str,
    park_key: str,
    window_phrase: str,
    plan_id: Optional[str] = None,
) -> bool:
    """Fire when a thunderstorm appears in the next-6-hour forecast that
    wasn't there on the previous poll, and the recipient has an active
    plan for the affected park today.

    Sibling of `alert_plan_disruption` — fires on the second axis of
    the agentic-loop story ("the system noticed something that
    invalidates your plan"). DOWN/UP transitions are the per-ride
    axis; this is the park-wide axis.

    Priority 1: actionable — Disney pauses outdoor rides for
    lightning, so re-sequencing the day before the storm hits is the
    play. Sticks with the same priority convention as went_down
    (also priority 1).

    `window_phrase` comes from weather.format_storm_window — keeps the
    body wording consistent with what the log line printed and avoids
    locale assumptions in the notifier.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    # Storm is plan-wide, not one ride — link to the whole day's adjust
    # page (drop outdoor rides / prioritize indoor).
    url = _replan_url(plan_id, None, "storm")
    title = f"{emoji} Storm forecast — plan may shift"
    tail = (
        "Tap to adjust — drop outdoor rides or move indoor ones up."
        if url
        else "Re-check with Claude to slot indoor rides ahead of the storm."
    )
    body = (
        f"{park_name}\n"
        f"Thunderstorm now in the forecast {window_phrase}. "
        f"Disney pauses outdoor rides for lightning. {tail}"
    )
    return _send(
        user_key, title, body, priority=1, url=url,
        url_title="Adjust for the storm" if url else None,
    )
