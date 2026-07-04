"""Next-up nudge decision logic (nudge.py) — clock injected throughout,
never datetime.now() (per the coverage standing orders)."""
from datetime import datetime, timezone, timedelta

import nudge

T0 = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


class TestShouldNudge:
    def test_fires_after_wait_plus_buffer(self):
        # predicted 30 + buffer 20 = due at 50 min.
        since = iso(T0 - timedelta(minutes=51))
        assert nudge.should_nudge(since, 30, held_ll=False, now=T0)

    def test_quiet_before_due_time(self):
        since = iso(T0 - timedelta(minutes=40))
        assert not nudge.should_nudge(since, 30, held_ll=False, now=T0)

    def test_held_ll_uses_short_wait_estimate(self):
        # Held LL: est 15 + buffer 20 = due at 35 min — fires where the
        # 30-min standby prediction (due at 50) still wouldn't.
        since = iso(T0 - timedelta(minutes=36))
        assert nudge.should_nudge(since, 30, held_ll=True, now=T0)
        assert not nudge.should_nudge(since, 30, held_ll=False, now=T0)

    def test_no_prediction_falls_back_to_default(self):
        # Default 30 + buffer 20 = 50.
        since = iso(T0 - timedelta(minutes=51))
        assert nudge.should_nudge(since, None, held_ll=False, now=T0)
        since_late = iso(T0 - timedelta(minutes=45))
        assert not nudge.should_nudge(since_late, None, held_ll=False, now=T0)

    def test_never_fires_without_timestamp(self):
        assert not nudge.should_nudge(None, 30, held_ll=False, now=T0)
        assert not nudge.should_nudge("", 30, held_ll=False, now=T0)
        assert not nudge.should_nudge("garbage", 30, held_ll=False, now=T0)

    def test_naive_timestamp_is_rejected_not_crashed(self):
        naive = (T0 - timedelta(minutes=90)).replace(tzinfo=None).isoformat()
        assert not nudge.should_nudge(naive, 30, held_ll=False, now=T0)

    def test_stale_next_up_never_nudges(self):
        # Older than MAX_AGE (180 min) — the family moved on.
        since = iso(T0 - timedelta(minutes=181))
        assert not nudge.should_nudge(since, 30, held_ll=False, now=T0)

    def test_zulu_suffix_parses(self):
        # The web stamps next_up_since with a trailing Z (toISOString).
        since = (T0 - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        assert nudge.should_nudge(since, 30, held_ll=False, now=T0)


class TestNudgeFiresAt:
    def test_anchor_plus_estimate_plus_buffer(self):
        since = T0 - timedelta(minutes=10)
        fires = nudge.nudge_fires_at(iso(since), 40, held_ll=False)
        assert fires == since + timedelta(minutes=60)

    def test_none_without_anchor(self):
        assert nudge.nudge_fires_at(None, 40, held_ll=False) is None


def _rides(*pairs):
    return [{"ride_id": i, "ride_name": n} for i, n in pairs]


class TestPickLlCandidate:
    RIDES = _rides(
        ("space", "Space Mountain"),
        ("tron", "TRON"),
        ("buzz", "Buzz Lightyear"),
    )

    def test_earliest_future_return_wins(self):
        lls = {
            "tron": {"return_start": iso(T0 + timedelta(hours=2)), "price": "$15"},
            "buzz": {"return_start": iso(T0 + timedelta(minutes=30))},
        }
        cand = nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0)
        assert cand["ride_id"] == "buzz"
        assert cand["ride_name"] == "Buzz Lightyear"

    def test_skips_held_rides_and_next_up(self):
        lls = {
            "space": {"return_start": iso(T0 + timedelta(minutes=10))},  # next_up
            "tron": {"return_start": iso(T0 + timedelta(minutes=20))},   # held
            "buzz": {"return_start": iso(T0 + timedelta(hours=3))},
        }
        cand = nudge.pick_ll_candidate(
            self.RIDES, {"tron": iso(T0 + timedelta(hours=1))}, lls, "space", T0
        )
        assert cand["ride_id"] == "buzz"

    def test_past_returns_are_unusable(self):
        lls = {"tron": {"return_start": iso(T0 - timedelta(minutes=5))}}
        assert nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0) is None

    def test_no_offers_returns_none(self):
        assert nudge.pick_ll_candidate(self.RIDES, {}, {}, "space", T0) is None

    def test_unparseable_offer_skipped(self):
        lls = {
            "tron": {"return_start": "not-a-time"},
            "buzz": {"return_start": iso(T0 + timedelta(minutes=30))},
        }
        cand = nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0)
        assert cand["ride_id"] == "buzz"

    def test_price_carried_through(self):
        lls = {"tron": {"return_start": iso(T0 + timedelta(minutes=30)), "price": "$12"}}
        cand = nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0)
        assert cand["price"] == "$12"

    def test_walk_on_short_standby_never_suggested(self):
        # Spaceship Earth class (2026-07-04): near-walk-on rides have the
        # earliest slots forever — an LL there isn't worth a booking.
        lls = {
            "tron": {"return_start": iso(T0 + timedelta(minutes=30))},
            "buzz": {"return_start": iso(T0 + timedelta(hours=2))},
        }
        waits = {"tron": 15, "buzz": 45}
        cand = nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0, waits)
        assert cand["ride_id"] == "buzz"  # tron's 15m standby disqualifies it

    def test_unknown_wait_still_suggestable(self):
        lls = {"tron": {"return_start": iso(T0 + timedelta(minutes=30))}}
        cand = nudge.pick_ll_candidate(self.RIDES, {}, lls, "space", T0, {})
        assert cand["ride_id"] == "tron"
