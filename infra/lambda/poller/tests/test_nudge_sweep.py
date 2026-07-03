"""Handler-level next-up nudge sweep: an overdue next_up fires ONE
combined push with the /done token and the best LL candidate, and marks
the (plan, ride) cooldown."""
from datetime import datetime, timedelta, timezone

import index


def _attr(ride_id, name, ll=None):
    return {
        "id": ride_id,
        "name": name,
        "status": "CLOSED",
        "wait_mins": None,
        "park_name": "Magic Kingdom",
        "park_key": "magic_kingdom",
        "last_seen": "2026-07-03T14:00:00+00:00",
        "ll": ll,
    }


class TestNudgeSweep:
    def test_overdue_next_up_sends_combined_nudge(self, monkeypatch):
        now = datetime.now(timezone.utc)
        plan = {
            "user_id": "megan",
            "plan_id": "p1",
            "park_key": "magic_kingdom",
            "rides": [
                {"ride_id": "space", "ride_name": "Space Mountain",
                 "predicted_wait_min": 30},
                {"ride_id": "tron", "ride_name": "TRON",
                 "predicted_wait_min": 45},
            ],
            "ll_holds": {},
            "next_up": "space",
            # 60 min ago: past predicted 30 + buffer 20, under max age.
            "next_up_since": (now - timedelta(minutes=60)).isoformat(),
            "done_token": "tok123",
        }
        ll_offer = {
            "return_start": (now + timedelta(minutes=30)).isoformat(),
            "price": "$15",
        }

        sent = []
        cooldowns = []

        def fake_fetch(park_key):
            # TRON's live LL offer is what the sweep should surface.
            return [_attr("tron", "TRON", ll=ll_offer)] if park_key == "magic_kingdom" else []

        monkeypatch.setattr(index.wait_times, "fetch_live_data", fake_fetch)
        monkeypatch.setattr(index.wait_times, "fetch_park_hours", lambda park: None)
        monkeypatch.setattr(index.db, "get_ride", lambda ride_id: None)
        monkeypatch.setattr(index.db, "upsert_ride", lambda attr: None)
        monkeypatch.setattr(index.db, "get_park_subscribers", lambda park: [])
        monkeypatch.setattr(
            index.db, "build_active_plan_ride_index",
            lambda date_iso, now_et=None: ({}, [plan]),
        )
        monkeypatch.setattr(index.weather, "fetch_forecast", lambda: None)
        monkeypatch.setattr(
            index.db, "get_user_profile",
            lambda uid: {"pushover_user_key": "PKEY"},
        )
        monkeypatch.setattr(index.db, "is_nudge_on_cooldown", lambda p, r: False)
        monkeypatch.setattr(
            index.db, "mark_nudge_sent",
            lambda p, r: cooldowns.append((p, r)),
        )
        monkeypatch.setattr(
            index.notifier, "alert_next_up_nudge",
            lambda key, **kw: (sent.append((key, kw)), True)[1],
        )

        result = index.handler({}, None)

        assert result["status"] == "ok"
        assert cooldowns == [("p1", "space")]
        assert len(sent) == 1
        key, kw = sent[0]
        assert key == "PKEY"
        assert kw["ride_name"] == "Space Mountain"
        assert kw["ride_id"] == "space"
        assert kw["done_token"] == "tok123"
        assert kw["ll_ride_name"] == "TRON"
        assert kw["ll_price"] == "$15"

    def test_fresh_next_up_stays_quiet(self, monkeypatch):
        now = datetime.now(timezone.utc)
        plan = {
            "user_id": "megan",
            "plan_id": "p1",
            "park_key": "magic_kingdom",
            "rides": [{"ride_id": "space", "ride_name": "Space Mountain",
                       "predicted_wait_min": 30}],
            "ll_holds": {},
            "next_up": "space",
            # Only 10 min in — nowhere near due.
            "next_up_since": (now - timedelta(minutes=10)).isoformat(),
            "done_token": "tok123",
        }
        sent = []
        monkeypatch.setattr(index.wait_times, "fetch_live_data", lambda park: [])
        monkeypatch.setattr(index.wait_times, "fetch_park_hours", lambda park: None)
        monkeypatch.setattr(index.db, "get_park_subscribers", lambda park: [])
        monkeypatch.setattr(
            index.db, "build_active_plan_ride_index",
            lambda date_iso, now_et=None: ({}, [plan]),
        )
        monkeypatch.setattr(index.weather, "fetch_forecast", lambda: None)
        monkeypatch.setattr(
            index.notifier, "alert_next_up_nudge",
            lambda key, **kw: (sent.append(kw), True)[1],
        )

        index.handler({}, None)
        assert sent == []
