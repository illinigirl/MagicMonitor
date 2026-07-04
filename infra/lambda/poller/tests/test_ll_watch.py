"""LL-watch improvement detector (index._ll_became_earlier)."""
import index


class TestLLBecameEarlier:
    def _ll(self, rs):
        return {"type": "free", "return_start": rs}

    def test_earlier_fires(self):
        assert index._ll_became_earlier(
            self._ll("2026-07-03T18:00:00-04:00"),
            self._ll("2026-07-03T15:00:00-04:00"),
        ) is True

    def test_later_does_not_fire(self):
        assert index._ll_became_earlier(
            self._ll("2026-07-03T15:00:00-04:00"),
            self._ll("2026-07-03T18:00:00-04:00"),
        ) is False

    def test_unchanged_does_not_fire(self):
        t = self._ll("2026-07-03T15:00:00-04:00")
        assert index._ll_became_earlier(t, dict(t)) is False

    def test_first_appearance_is_not_earlier(self):
        # No prior return time → not an "earlier" event (deferred signal).
        assert index._ll_became_earlier(None, self._ll("2026-07-03T15:00:00-04:00")) is False
        assert index._ll_became_earlier({}, self._ll("2026-07-03T15:00:00-04:00")) is False

    def test_offer_gone_does_not_fire(self):
        assert index._ll_became_earlier(self._ll("2026-07-03T15:00:00-04:00"), None) is False

    def test_malformed_is_safe(self):
        assert index._ll_became_earlier(self._ll("garbage"), self._ll("2026-07-03T15:00:00-04:00")) is False


class TestLLMinImprovement:
    """A trivial improvement (5 min earlier) must NOT fire — only a
    meaningful one (>= LL_MIN_IMPROVEMENT_MIN, default 20)."""

    def _ll(self, rs):
        return {"type": "free", "return_start": rs}

    def test_five_minutes_earlier_does_not_fire(self):
        assert index._ll_became_earlier(
            self._ll("2026-07-03T18:00:00-04:00"),
            self._ll("2026-07-03T17:55:00-04:00"),
        ) is False

    def test_big_improvement_fires(self):
        assert index._ll_became_earlier(
            self._ll("2026-07-03T18:00:00-04:00"),
            self._ll("2026-07-03T17:30:00-04:00"),
        ) is True


class TestHeldLLPrecision:
    """The held-LL precision gate (index-level): an available slot that
    doesn't beat what you hold is noise. Tested via the pure comparison
    the poller inlines."""

    def _parse(self, s):
        return index._parse_iso(s)

    def test_available_later_than_held_is_not_useful(self):
        # Hold 3pm; available 7:55pm (improved from 8pm) — still later.
        avail = self._parse("2026-07-03T19:55:00-04:00")
        held = self._parse("2026-07-03T15:00:00-04:00")
        assert not (avail < held)  # gate suppresses

    def test_available_earlier_than_held_is_useful(self):
        avail = self._parse("2026-07-03T14:00:00-04:00")
        held = self._parse("2026-07-03T15:00:00-04:00")
        assert avail < held  # gate fires


# ─── LL reappearance + short-standby suppression (2026-07-04) ─────────


class TestLlReappeared:
    def test_no_offer_to_offer_is_reappearance(self):
        assert index._ll_reappeared(None, {"return_start": "2026-07-04T16:40:00-04:00"})
        assert index._ll_reappeared({}, {"return_start": "2026-07-04T16:40:00-04:00"})

    def test_existing_offer_is_not_reappearance(self):
        # An offer that merely CHANGED belongs to the earlier-LL rule.
        assert not index._ll_reappeared(
            {"return_start": "2026-07-04T18:00:00-04:00"},
            {"return_start": "2026-07-04T16:40:00-04:00"},
        )

    def test_offer_disappearing_is_not_an_event(self):
        assert not index._ll_reappeared(
            {"return_start": "2026-07-04T16:40:00-04:00"}, None
        )

    def test_unparseable_new_offer_never_fires(self):
        assert not index._ll_reappeared(None, {"return_start": "soon-ish"})


class TestLlWorthAlerting:
    def test_short_standby_suppresses(self):
        # Mission: Space at 15m — an LL alert is noise, walk on instead.
        assert not index._ll_worth_alerting("OPERATING", 15)
        assert not index._ll_worth_alerting("OPERATING", 25)  # at threshold

    def test_long_standby_alerts(self):
        assert index._ll_worth_alerting("OPERATING", 26)
        assert index._ll_worth_alerting("OPERATING", 90)

    def test_down_or_unknown_wait_still_alerts(self):
        # No standby to walk onto — the LL info is genuinely useful.
        assert index._ll_worth_alerting("DOWN", None)
        assert index._ll_worth_alerting("OPERATING", None)
