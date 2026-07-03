"""Tests for notifier._send error containment.

The 2026-06-11 fix: _get_app_token() (a lazy SSM fetch on the first alert
a container sends) is now INSIDE _send's try block. A cold-start SSM
throttle/timeout there must be contained to this one send (return False),
not raised out of the per-attraction loop — which would abort the whole
poll after the DOWN cooldown was already marked, losing the alert for the
full cooldown window on the EventBridge retry.
"""

import notifier


class _FakeResp:
    def raise_for_status(self):
        pass


def test_token_fetch_failure_is_contained(monkeypatch):
    def boom():
        raise RuntimeError("SSM throttled on cold start")

    monkeypatch.setattr(notifier, "_get_app_token", boom)
    # Must NOT raise — returns False so the poll continues.
    assert notifier._send("user-key-123", "title", "message") is False


def test_happy_path_still_posts(monkeypatch):
    monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
    sent = {}

    def fake_post(url, data=None, timeout=None):
        sent["data"] = data
        return _FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    assert notifier._send("user-key-123", "title", "message", priority=1) is True
    assert sent["data"]["token"] == "tok"
    assert sent["data"]["priority"] == 1


def test_post_failure_returns_false(monkeypatch):
    monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")

    def fake_post(url, data=None, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    assert notifier._send("user-key-123", "title", "message") is False


def test_alert_plan_low_wait_message(monkeypatch):
    """Plan-aware low-wait: names the plan context, carries the wait +
    baseline numbers, sends at priority 0 (opportunity, not disruption)."""
    monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
    sent = {}

    def fake_post(url, data=None, timeout=None):
        sent.update(data)
        return _FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    ok = notifier.alert_plan_low_wait(
        "user-key", ride_name="Space Mountain", park_name="Magic Kingdom",
        park_key="magic_kingdom", wait_mins=15, typical_wait_mins=40,
        plan_id="PLAN#p1",
    )
    assert ok is True
    assert "Plan opportunity" in sent["title"]
    assert "15 min" in sent["message"]
    assert "in your plan today" in sent["message"]
    assert "~40 min" in sent["message"]
    assert sent["priority"] == 0


class TestAlertLLEarlier:
    """Earlier-LL push: message shape + framing (plan vs watch)."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
        sent = {}

        def fake_post(url, data=None, timeout=None):
            sent["data"] = data
            return _FakeResp()

        monkeypatch.setattr(notifier.requests, "post", fake_post)
        return sent

    def test_plan_framed_with_prior_and_price(self, monkeypatch):
        sent = self._capture(monkeypatch)
        ok = notifier.alert_ll_earlier(
            "key", ride_name="TRON", park_name="Magic Kingdom",
            park_key="magic_kingdom",
            new_return_start="2026-07-03T14:15:00-04:00",
            prior_return_start="2026-07-03T18:40:00-04:00",
            in_plan=True, price="$18",
        )
        assert ok is True
        msg = sent["data"]["message"]
        assert "2:15 PM" in sent["data"]["title"]
        assert "was 6:40 PM, now 2:15 PM" in msg
        assert "in your plan today" in msg
        assert "$18" in msg

    def test_watch_framed_no_prior(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_ll_earlier(
            "key", ride_name="Remy", park_name="EPCOT", park_key="epcot",
            new_return_start="2026-07-03T13:20:00-04:00",
        )
        msg = sent["data"]["message"]
        assert "on your watch list" in msg
        assert "1:20 PM" in msg

    def test_unparseable_time_degrades(self, monkeypatch):
        self._capture(monkeypatch)
        # Must not raise on a garbage timestamp.
        assert notifier.alert_ll_earlier(
            "key", ride_name="X", park_name="P", park_key="epcot",
            new_return_start="not-a-time",
        ) is True


class TestReplanDeepLink:
    """went_down disruption alerts carry a /replan deep-link when they
    have both plan_id + ride_id; back_up alerts don't (nothing to
    approve)."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
        sent = {}
        monkeypatch.setattr(
            notifier.requests, "post",
            lambda url, data=None, timeout=None: (sent.update(data=data) or _FakeResp()),
        )
        return sent

    def test_went_down_includes_replan_url(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_disruption(
            "key", ride_name="Space Mountain", park_name="Magic Kingdom",
            park_key="magic_kingdom", disruption_type="went_down",
            plan_id="p1", ride_id="sm",
        )
        url = sent["data"]["url"]
        assert "/replan?plan=p1" in url and "ride=sm" in url
        assert sent["data"]["url_title"] == "Drop it or re-plan"

    def test_back_up_links_do_next(self, monkeypatch):
        # Any plan alert can trigger a re-plan: back-up now links too,
        # with a "do it next" framing (type=next) rather than "down".
        sent = self._capture(monkeypatch)
        notifier.alert_plan_disruption(
            "key", ride_name="Space Mountain", park_name="Magic Kingdom",
            park_key="magic_kingdom", disruption_type="back_up",
            plan_id="p1", ride_id="sm", wait_mins=20,
        )
        assert "/replan?plan=p1" in sent["data"]["url"]
        assert "type=next" in sent["data"]["url"]

    def test_went_down_without_ids_stays_informational(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_disruption(
            "key", ride_name="X", park_name="MK", park_key="magic_kingdom",
            disruption_type="went_down",
        )
        assert "url" not in sent["data"]


class TestOpportunityAndStormDeepLinks:
    """Every plan alert links to /replan so it's an actionable re-plan
    entry point. Opportunity alerts suggest 'do next'; storm links to the
    plan (no ride); a favorites-only LL watcher (not in a plan) gets no
    link since there's no plan to re-sequence."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
        sent = {}
        monkeypatch.setattr(
            notifier.requests, "post",
            lambda url, data=None, timeout=None: (sent.update(data=data) or _FakeResp()),
        )
        return sent

    def test_plan_low_wait_links_do_next(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_low_wait(
            "key", ride_name="Space Mountain", park_name="MK",
            park_key="magic_kingdom", wait_mins=10, plan_id="p1", ride_id="sm",
        )
        assert "/replan?plan=p1" in sent["data"]["url"] and "ride=sm" in sent["data"]["url"]
        assert "type=next" in sent["data"]["url"]

    def test_ll_earlier_in_plan_links(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_ll_earlier(
            "key", ride_name="TRON", park_name="MK", park_key="magic_kingdom",
            new_return_start="2026-07-03T14:00:00-04:00", in_plan=True,
            plan_id="p1", ride_id="tron",
        )
        assert "/replan?plan=p1" in sent["data"]["url"]

    def test_ll_earlier_favorite_only_no_link(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_ll_earlier(
            "key", ride_name="TRON", park_name="MK", park_key="magic_kingdom",
            new_return_start="2026-07-03T14:00:00-04:00", in_plan=False,
            plan_id=None, ride_id="tron",
        )
        assert "url" not in sent["data"]

    def test_storm_links_plan_no_ride(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_weather_shift(
            "key", park_name="MK", park_key="magic_kingdom",
            window_phrase="this afternoon", plan_id="p1",
        )
        url = sent["data"]["url"]
        assert "/replan?plan=p1" in url and "type=storm" in url and "ride=" not in url


class TestAlertPlanDrift:
    """Aggregated plan-drift nudge: direction-aware copy + /replan link."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
        sent = {}
        monkeypatch.setattr(
            notifier.requests, "post",
            lambda url, data=None, timeout=None: (sent.update(data=data) or _FakeResp()),
        )
        return sent

    def test_lighter_says_ahead_and_links(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_drift(
            "key", park_name="EPCOT", park_key="epcot", net_minutes=65, plan_id="p1",
        )
        assert "ahead of plan" in sent["data"]["title"].lower() or "ahead" in sent["data"]["title"]
        assert "~65 min under" in sent["data"]["message"]
        assert "/replan?plan=p1" in sent["data"]["url"] and "type=drift" in sent["data"]["url"]

    def test_heavier_says_busier(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_drift(
            "key", park_name="EPCOT", park_key="epcot", net_minutes=-75, plan_id="p1",
        )
        assert "busier" in sent["data"]["title"].lower()
        assert "~75 min over" in sent["data"]["message"]
