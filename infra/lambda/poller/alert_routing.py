"""
Alert recipient resolution for the poller.

For a given ride event (e.g., RIDE X went DOWN), multiple "alert
sources" can claim the same user:

  - The user favorited this ride → fire alert_ride_down (generic).
  - The user has this ride in TODAY's active plan → fire
    alert_plan_disruption (references the plan, more actionable).

The user should get exactly ONE alert per event, and it should be
the MOST actionable one available. Before this module existed, the
two sources were dispatched in series with a manual
`if user in favoriter_set: continue` skip, which silently picked
whichever source ran first (the favoriter alert) instead of whichever
was most actionable (the plan alert).

This module makes the priority order explicit and one-place-only:
each source contributes candidates with a priority; the resolver
picks the highest-priority candidate per user. Adding a new alert
source = adding a new priority constant + appending candidates,
without touching the existing sources' dispatch code.

The scope here is deliberately small. Cooldowns, park-hours gating,
and the actual notifier-side send (Pushover HTTP) all happen
*around* the resolver, not inside it. This module just answers
"given these candidates, who gets which alert?"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


# Priority constants. Higher = more specific / more actionable; wins
# ties when multiple sources match the same user for the same event.
#
# When adding a new source, place its priority relative to these:
# more-actionable signals (anything plan-aware, anything mid-trip)
# belong above PRIORITY_FAVORITE; generic park/ride subscriptions
# belong at or below PRIORITY_FAVORITE.
PRIORITY_PLAN = 100
PRIORITY_FAVORITE = 50


@dataclass(frozen=True)
class AlertCandidate:
    """One alert source claiming a user should receive an alert.

    notifier_fn + kwargs are dispatched as `notifier_fn(user_key,
    **kwargs)` by the caller after the resolver picks a winner.
    Embedding the notifier directly (rather than a string discriminator)
    keeps the dispatch site free of a registry — the trade-off is
    that each call site has to know the right notifier function to
    pair with each priority tier, which is reasonable at the current
    handful of alert types. If the catalog grows past ~6-8 types,
    revisit and consider a discriminator + registry.
    """

    user_id: str
    priority: int
    notifier_fn: Callable[..., bool]
    kwargs: dict[str, Any]


def resolve_alert_recipients(
    candidates: list[AlertCandidate],
) -> dict[str, AlertCandidate]:
    """Pick one alert per user — highest priority wins.

    Ties (same user, same priority from two sources) are broken by
    first-arrival, so call-site ordering provides a stable secondary
    rule. In practice ties shouldn't happen at the priority levels
    we use today (each priority tier corresponds to one source).

    Returns a dict keyed by user_id so the caller can dispatch with
    a simple iteration. Empty input → empty dict.
    """
    best: dict[str, AlertCandidate] = {}
    for c in candidates:
        existing = best.get(c.user_id)
        if existing is None or c.priority > existing.priority:
            best[c.user_id] = c
    return best
