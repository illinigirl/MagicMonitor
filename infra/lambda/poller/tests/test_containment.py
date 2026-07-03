"""Per-ride containment: one attraction's failure must not abort the poll.

Regression for the 2026-07-03 held-LL None crash — a TypeError in one
ride's alert body propagated out of the per-attraction loop and took
ALL alerts dark for every ride and park behind it. The handler now
wraps each attraction's processing in try/except: log + continue.
"""
import index


def _attr(ride_id, name):
    return {
        "id": ride_id,
        "name": name,
        "status": "CLOSED",
        "wait_mins": None,
        "park_name": "Magic Kingdom",
        "park_key": "magic_kingdom",
        "last_seen": "2026-07-03T14:00:00+00:00",
        "ll": None,
    }


class TestPerRideContainment:
    def test_one_rides_crash_does_not_abort_the_poll(self, monkeypatch, capsys):
        processed = []

        def fake_fetch(park_key):
            # One park with a crashing ride FIRST, then a healthy one —
            # ordering matters: the healthy ride sits in the blast radius.
            if park_key == "magic_kingdom":
                return [_attr("bad", "Crashy Coaster"), _attr("good", "Fine Flume")]
            return []

        def fake_upsert(attr):
            if attr["id"] == "bad":
                raise TypeError("'NoneType' object is not subscriptable")
            processed.append(attr["id"])

        monkeypatch.setattr(index.wait_times, "fetch_live_data", fake_fetch)
        monkeypatch.setattr(index.db, "get_ride", lambda ride_id: None)
        monkeypatch.setattr(index.db, "upsert_ride", fake_upsert)
        monkeypatch.setattr(index.db, "get_park_subscribers", lambda park: [])
        monkeypatch.setattr(
            index.db, "build_active_plan_ride_index",
            lambda date_iso, now_et=None: ({}, []),
        )

        result = index.handler({}, None)

        # The poll completed and the ride AFTER the crash was processed.
        assert result["status"] == "ok"
        assert processed == ["good"]
        # The failure was logged with the ride's name + containment marker.
        out = capsys.readouterr().out
        assert "Crashy Coaster" in out
        assert "contained" in out
