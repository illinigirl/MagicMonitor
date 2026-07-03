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
