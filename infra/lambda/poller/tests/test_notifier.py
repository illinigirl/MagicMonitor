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
