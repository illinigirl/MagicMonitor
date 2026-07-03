"""
Tests for alert_routing.resolve_alert_recipients — the small pure
function that decides "which alert does each user get" when multiple
sources (favoriter, plan-aware) match the same user for one event.

The regression these tests guard against: prior to the refactor on
2026-05-24, a user who was BOTH a favoriter AND had the ride in
their active plan received the favoriter alert because favoriter
dispatch ran first; the plan-aware fanout's `if user in
favoriter_set: continue` silently skipped the more-actionable plan
message. These tests pin the priority order so that pattern can't
silently come back.
"""

import alert_routing
from alert_routing import (
    AlertCandidate,
    PRIORITY_FAVORITE,
    PRIORITY_PLAN,
    resolve_alert_recipients,
)


def _fake_notifier(user_key, **kwargs):
    """Sentinel notifier function — not invoked by the resolver,
    just stored on the candidate so we can identify which alert
    would dispatch."""
    return True


def _plan_candidate(user_id: str, plan_id: str = "plan-x") -> AlertCandidate:
    return AlertCandidate(
        user_id=user_id,
        priority=PRIORITY_PLAN,
        notifier_fn=_fake_notifier,
        kwargs={"alert_kind": "plan_disruption", "plan_id": plan_id},
    )


def _favorite_candidate(user_id: str) -> AlertCandidate:
    return AlertCandidate(
        user_id=user_id,
        priority=PRIORITY_FAVORITE,
        notifier_fn=_fake_notifier,
        kwargs={"alert_kind": "ride_down"},
    )


class TestResolveAlertRecipients:
    def test_empty_input_returns_empty_dict(self):
        assert resolve_alert_recipients([]) == {}

    def test_single_candidate_passes_through(self):
        c = _favorite_candidate("alice")
        result = resolve_alert_recipients([c])
        assert result == {"alice": c}

    def test_plan_beats_favorite_for_same_user(self):
        """The exact regression: user is both favoriter AND plan
        target. Plan-aware alert is the more actionable one and
        must win."""
        plan = _plan_candidate("alice", plan_id="mk-2026-05-24")
        fav = _favorite_candidate("alice")

        # Order them favorite-first to prove order doesn't matter.
        result = resolve_alert_recipients([fav, plan])

        assert result == {"alice": plan}
        assert result["alice"].kwargs["alert_kind"] == "plan_disruption"

    def test_plan_beats_favorite_regardless_of_input_order(self):
        plan = _plan_candidate("alice")
        fav = _favorite_candidate("alice")

        plan_first = resolve_alert_recipients([plan, fav])
        fav_first = resolve_alert_recipients([fav, plan])

        assert plan_first == fav_first == {"alice": plan}

    def test_favorite_only_user_gets_favorite_alert(self):
        fav = _favorite_candidate("bob")
        result = resolve_alert_recipients([fav])
        assert result == {"bob": fav}
        assert result["bob"].kwargs["alert_kind"] == "ride_down"

    def test_plan_only_user_gets_plan_alert(self):
        plan = _plan_candidate("carol", plan_id="ep-2026-05-24")
        result = resolve_alert_recipients([plan])
        assert result == {"carol": plan}
        assert result["carol"].kwargs["plan_id"] == "ep-2026-05-24"

    def test_distinct_users_all_retained_each_with_correct_alert(self):
        """A composite case mirroring the real DOWN-path scenario:
        alice is both favoriter + plan, bob is favoriter only,
        carol is plan only. Resolver should produce three entries
        with the right kind for each."""
        plan_a = _plan_candidate("alice", plan_id="mk-2026-05-24")
        fav_a = _favorite_candidate("alice")
        fav_b = _favorite_candidate("bob")
        plan_c = _plan_candidate("carol", plan_id="ak-2026-05-24")

        result = resolve_alert_recipients([plan_a, fav_a, fav_b, plan_c])

        assert set(result.keys()) == {"alice", "bob", "carol"}
        assert result["alice"].kwargs["alert_kind"] == "plan_disruption"
        assert result["alice"].kwargs["plan_id"] == "mk-2026-05-24"
        assert result["bob"].kwargs["alert_kind"] == "ride_down"
        assert result["carol"].kwargs["alert_kind"] == "plan_disruption"
        assert result["carol"].kwargs["plan_id"] == "ak-2026-05-24"

    def test_same_priority_ties_broken_by_first_arrival(self):
        """Stable secondary ordering. Not expected to happen in
        practice (each priority tier maps to one source today) but
        worth pinning so a future tie doesn't introduce
        nondeterminism."""
        first = AlertCandidate(
            user_id="alice", priority=PRIORITY_FAVORITE,
            notifier_fn=_fake_notifier, kwargs={"tag": "first"},
        )
        second = AlertCandidate(
            user_id="alice", priority=PRIORITY_FAVORITE,
            notifier_fn=_fake_notifier, kwargs={"tag": "second"},
        )
        result = resolve_alert_recipients([first, second])
        assert result["alice"].kwargs["tag"] == "first"

    def test_priority_constants_have_expected_ordering(self):
        """Pins the absolute relationship: plan > favorite. If a
        future refactor accidentally swaps these, every dispatch
        flips silently — this assertion makes that loud."""
        assert PRIORITY_PLAN > PRIORITY_FAVORITE


class TestBuildCandidatesShape:
    """End-to-end shape check mirroring how the DOWN path in index.py
    constructs candidates. Doesn't invoke the real notifier or DDB —
    just verifies that the candidate construction pattern produces
    a list the resolver consumes correctly."""

    def test_back_up_path_candidate_construction_resolves_correctly(self):
        """Same regression-shape check as the DOWN-path test, but
        for the BACK_UP branch (ride was DOWN, now OPERATING).
        Plan-aware alert kwargs include wait_mins; favoriter alert
        kwargs include actual_downtime_mins. Resolver still picks
        plan over favoriter for users matching both."""
        plan_targets = [
            ("alice", "PLAN#mk-2026-05-24T09:00:00+00:00"),
            ("carol", "PLAN#ak-2026-05-24T09:00:00+00:00"),
        ]
        favoriter_ids = ["alice", "bob"]

        ride_name = "TRON Lightcycle / Run"
        park_name = "Magic Kingdom"
        park_key = "magic_kingdom"
        new_wait = 65
        actual_downtime_mins = 45

        candidates: list[AlertCandidate] = []
        for user_id, plan_id in plan_targets:
            candidates.append(AlertCandidate(
                user_id=user_id,
                priority=PRIORITY_PLAN,
                notifier_fn=_fake_notifier,
                kwargs={
                    "ride_name": ride_name,
                    "park_name": park_name,
                    "park_key": park_key,
                    "disruption_type": "back_up",
                    "plan_id": plan_id,
                    "wait_mins": new_wait,
                },
            ))
        for user_id in favoriter_ids:
            candidates.append(AlertCandidate(
                user_id=user_id,
                priority=PRIORITY_FAVORITE,
                notifier_fn=_fake_notifier,
                kwargs={
                    "ride_name": ride_name,
                    "park_name": park_name,
                    "park_key": park_key,
                    "wait_mins": new_wait,
                    "actual_downtime_mins": actual_downtime_mins,
                },
            ))

        resolved = resolve_alert_recipients(candidates)

        assert set(resolved.keys()) == {"alice", "bob", "carol"}

        # Alice (dual-source) gets the plan-disruption back_up alert
        # with the plan reference, not the generic actual-downtime
        # blurb.
        assert resolved["alice"].kwargs["disruption_type"] == "back_up"
        assert resolved["alice"].kwargs["plan_id"] == (
            "PLAN#mk-2026-05-24T09:00:00+00:00"
        )
        assert "actual_downtime_mins" not in resolved["alice"].kwargs

        # Bob (favoriter only) gets the generic UP alert with the
        # downtime stat and current wait, no plan reference.
        assert resolved["bob"].kwargs["actual_downtime_mins"] == 45
        assert "plan_id" not in resolved["bob"].kwargs
        assert "disruption_type" not in resolved["bob"].kwargs

        # Carol (plan only) gets the plan alert.
        assert resolved["carol"].kwargs["disruption_type"] == "back_up"

    def test_down_path_candidate_construction_resolves_correctly(self):
        # Inputs as the DOWN path would have them after calling
        # db.lookup_plan_targets and filter_to_favoriters.
        plan_targets = [
            ("alice", "PLAN#mk-2026-05-24T09:00:00+00:00"),
            ("carol", "PLAN#ak-2026-05-24T09:00:00+00:00"),
        ]
        favoriter_ids = ["alice", "bob"]  # alice overlaps with plan

        ride_name = "Haunted Mansion"
        park_name = "Magic Kingdom"
        park_key = "magic_kingdom"

        candidates: list[AlertCandidate] = []
        for user_id, plan_id in plan_targets:
            candidates.append(AlertCandidate(
                user_id=user_id,
                priority=PRIORITY_PLAN,
                notifier_fn=_fake_notifier,
                kwargs={
                    "ride_name": ride_name,
                    "park_name": park_name,
                    "park_key": park_key,
                    "disruption_type": "went_down",
                    "plan_id": plan_id,
                },
            ))
        for user_id in favoriter_ids:
            candidates.append(AlertCandidate(
                user_id=user_id,
                priority=PRIORITY_FAVORITE,
                notifier_fn=_fake_notifier,
                kwargs={
                    "ride_name": ride_name,
                    "park_name": park_name,
                    "park_key": park_key,
                },
            ))

        resolved = resolve_alert_recipients(candidates)

        # Three distinct recipients despite the overlap.
        assert set(resolved.keys()) == {"alice", "bob", "carol"}

        # Alice — the dual-source user — gets the plan alert.
        assert resolved["alice"].kwargs["disruption_type"] == "went_down"
        assert resolved["alice"].kwargs["plan_id"] == (
            "PLAN#mk-2026-05-24T09:00:00+00:00"
        )

        # Bob — favorite only — gets the generic alert (no plan_id).
        assert "plan_id" not in resolved["bob"].kwargs
        assert "disruption_type" not in resolved["bob"].kwargs

        # Carol — plan only — gets the plan alert.
        assert resolved["carol"].kwargs["disruption_type"] == "went_down"


class TestLowWaitScenario:
    """Plan-aware LOW WAIT (added 2026-07-03) uses the same resolver as
    DOWN/BACK UP: a user with the ride in today's active plan gets the
    plan-framed opportunity alert; a favoriter-only user gets the generic
    low-wait alert; a dual-source user gets the plan version."""

    def test_plan_framing_beats_favorite_for_low_wait(self):
        low_wait_kwargs = {
            "ride_name": "Space Mountain",
            "park_name": "Magic Kingdom",
            "park_key": "magic_kingdom",
            "wait_mins": 15,
            "typical_wait_mins": 40,
            "forecast_wait_mins": None,
        }
        candidates = [
            AlertCandidate(
                user_id="alice",  # dual-source: plan + favorite
                priority=PRIORITY_PLAN,
                notifier_fn=_fake_notifier,
                kwargs={**low_wait_kwargs, "plan_id": "PLAN#p1"},
            ),
            AlertCandidate(
                user_id="alice",
                priority=PRIORITY_FAVORITE,
                notifier_fn=_fake_notifier,
                kwargs=dict(low_wait_kwargs),
            ),
            AlertCandidate(
                user_id="bob",  # favorite only
                priority=PRIORITY_FAVORITE,
                notifier_fn=_fake_notifier,
                kwargs=dict(low_wait_kwargs),
            ),
        ]
        resolved = resolve_alert_recipients(candidates)
        assert set(resolved.keys()) == {"alice", "bob"}
        assert resolved["alice"].kwargs["plan_id"] == "PLAN#p1"  # plan wins
        assert "plan_id" not in resolved["bob"].kwargs
