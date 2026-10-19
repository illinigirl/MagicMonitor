"""Who hears about a Lightning Lane offer (index.build_ll_wanted /
plan_ll_recipients).

The bug (reported 2026-10-19): Megan moved her Lightning Lane off
Pirates, and then kept getting "earlier LL" pushes for Pirates.

Two things combined:

  1. Plan membership ALONE qualified a user for LL alerts. Pirates was
     still in the plan, so it kept alerting — there was no way to say
     "I'm riding this standby."
  2. Clearing the hold is what STARTED the noise. `_ll_beats_held` in
     handler() is the "only tell me if it beats what you hold" filter,
     and it only exists while a hold exists. No hold meant no filter,
     not silence.

The fix gates in-plan recipients on actual LL intent: a held LL, or the
ride marked `ll_planned`. Neither → standby → quiet.

The fanout had ZERO coverage before this file; test_ll_watch.py tests
only the pure event detectors (_ll_became_earlier etc.), not who
receives anything. Per the CLAUDE.md standing orders, the decision
logic was extracted out of handler() so it could be tested at all.
"""

import index


# ─── build_ll_wanted ────────────────────────────────────────────────


class TestBuildLlWanted:
    def test_held_ll_wants_alerts(self):
        plans = [{"user_id": "megan", "ll_holds": {"pirates": "2026-10-19T15:00:00-04:00"},
                  "rides": [{"ride_id": "pirates", "ride_name": "Pirates"}]}]
        assert index.build_ll_wanted(plans) == {("megan", "pirates")}

    def test_ll_planned_wants_alerts(self):
        """The MLL-hunting case: no hold yet, but the plan intends one.
        This is the whole reason reappearance alerts exist (2026-07-04)
        and must keep working."""
        plans = [{"user_id": "megan", "ll_holds": {},
                  "rides": [{"ride_id": "tron", "ride_name": "TRON",
                             "ll_planned": True}]}]
        assert index.build_ll_wanted(plans) == {("megan", "tron")}

    def test_plan_ride_with_neither_is_standby(self):
        """THE reported case: in the plan, no hold, no ll_planned."""
        plans = [{"user_id": "megan", "ll_holds": {},
                  "rides": [{"ride_id": "pirates", "ride_name": "Pirates"}]}]
        assert index.build_ll_wanted(plans) == set()

    def test_clearing_a_hold_makes_the_ride_standby(self):
        """Directly models what Megan did: the ride stays in the plan,
        the hold goes away. Before the fix this is where the alerts
        started instead of stopping."""
        with_hold = [{"user_id": "megan",
                      "ll_holds": {"pirates": "2026-10-19T15:00:00-04:00"},
                      "rides": [{"ride_id": "pirates", "ride_name": "Pirates"}]}]
        cleared = [{"user_id": "megan", "ll_holds": {},
                    "rides": [{"ride_id": "pirates", "ride_name": "Pirates"}]}]
        assert index.build_ll_wanted(with_hold) == {("megan", "pirates")}
        assert index.build_ll_wanted(cleared) == set()

    def test_ll_planned_survives_clearing_the_hold(self):
        """Still hunting an LL after dropping the booked one — must stay
        subscribed. This is the case the fix must NOT break."""
        plans = [{"user_id": "megan", "ll_holds": {},
                  "rides": [{"ride_id": "pirates", "ride_name": "Pirates",
                             "ll_planned": True}]}]
        assert index.build_ll_wanted(plans) == {("megan", "pirates")}

    def test_is_per_user_not_per_ride(self):
        """One person hunting an LL for a ride another is walking on."""
        plans = [
            {"user_id": "megan", "ll_holds": {},
             "rides": [{"ride_id": "pirates", "ll_planned": True}]},
            {"user_id": "court", "ll_holds": {},
             "rides": [{"ride_id": "pirates"}]},
        ]
        assert index.build_ll_wanted(plans) == {("megan", "pirates")}

    def test_mixed_rides_in_one_plan(self):
        plans = [{"user_id": "megan",
                  "ll_holds": {"sdmt": "2026-10-19T13:00:00-04:00"},
                  "rides": [{"ride_id": "sdmt"},
                            {"ride_id": "tron", "ll_planned": True},
                            {"ride_id": "pirates"}]}]
        assert index.build_ll_wanted(plans) == {
            ("megan", "sdmt"), ("megan", "tron")
        }

    # -- degenerate / defensive --

    def test_no_plans(self):
        assert index.build_ll_wanted([]) == set()

    def test_none_is_tolerated(self):
        assert index.build_ll_wanted(None) == set()

    def test_plan_without_user_id_is_skipped(self):
        plans = [{"ll_holds": {"pirates": "x"},
                  "rides": [{"ride_id": "pirates", "ll_planned": True}]}]
        assert index.build_ll_wanted(plans) == set()

    def test_ride_without_ride_id_is_skipped(self):
        plans = [{"user_id": "megan", "ll_holds": {},
                  "rides": [{"ride_name": "no id", "ll_planned": True}]}]
        assert index.build_ll_wanted(plans) == set()

    def test_missing_keys_do_not_raise(self):
        assert index.build_ll_wanted([{"user_id": "megan"}]) == set()

    def test_falsy_ll_planned_is_standby(self):
        for falsy in (False, None, 0, ""):
            plans = [{"user_id": "megan", "ll_holds": {},
                      "rides": [{"ride_id": "pirates", "ll_planned": falsy}]}]
            assert index.build_ll_wanted(plans) == set(), falsy


# ─── plan_ll_recipients ─────────────────────────────────────────────


class TestPlanLlRecipients:
    WANTED = {("megan", "tron"), ("court", "pirates")}

    def test_only_wanting_users_receive(self):
        pid = {"megan": "p1", "court": "p2"}
        assert index.plan_ll_recipients(pid, self.WANTED, "tron") == {"megan"}

    def test_other_ride_other_recipient(self):
        pid = {"megan": "p1", "court": "p2"}
        assert index.plan_ll_recipients(pid, self.WANTED, "pirates") == {"court"}

    def test_nobody_wants_it(self):
        """All-standby party: an LL offer reaches no one. The caller must
        then NOT burn the ride's cooldown — see handler()."""
        pid = {"megan": "p1", "court": "p2"}
        assert index.plan_ll_recipients(pid, self.WANTED, "sdmt") == set()

    def test_user_not_in_plan_is_not_added(self):
        """ll_wanted is not a subscription list — it only narrows the
        in-plan set, never widens it."""
        assert index.plan_ll_recipients({}, self.WANTED, "tron") == set()

    def test_empty_wanted_suppresses_everyone(self):
        pid = {"megan": "p1"}
        assert index.plan_ll_recipients(pid, set(), "tron") == set()

    def test_none_inputs_do_not_raise(self):
        assert index.plan_ll_recipients(None, set(), "tron") == set()
