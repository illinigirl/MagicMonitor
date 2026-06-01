"""Tests for the read-side analytics tools ported to server_http (session 2.5).

Three things to cover that are unique to the HTTP port (the tool logic
itself is duplicated from server.py and exercised there + by evals):

1. The S3-backed snapshot loader: fetches once, caches for the
   container lifetime, and raises _SnapshotUnavailable (not a 500) when
   S3 is unreachable or unconfigured.
2. Graceful degradation: snapshot-backed tools return the
   "analytics temporarily unavailable" payload — not an exception —
   when the snapshot can't be loaded.
3. The pure-function tool bodies (heatmap filtering, ride-finder
   sort/filter, cluster summary stats, baseline lookup) against a small
   in-memory fixture, with the snapshot loader stubbed out.

The live DDB tools (forecast, downtime) are covered only for their
input-validation + resolution-failure paths here — their happy path
needs DDB and is the same query shape server.py already ships.
"""

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Env the OAuth metadata helpers read at import — mirror the existing
# server_http test so importing the module doesn't blow up.
os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import server_http  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────

SNAPSHOT_FIXTURE = {
    "heatmaps": {
        "magic_kingdom": [
            {"dow": 0, "hour": 10, "wait": 30, "n": 50},
            {"dow": 1, "hour": 11, "wait": 40, "n": 60},
        ],
    },
    "rides": [
        {
            "ride_name": "Space Mountain",
            "ride_id": "sm",
            "park_key": "magic_kingdom",
            "downtime_pct": 5.0,
            "avg_wait": 45,
            "max_wait": 90,
            "total_polls": 1000,
            "dow_hourly": [
                {"dow": 0, "hour": 10, "downtime_pct": 2, "n_active": 40, "wait": 35},
            ],
            # Two long (>=120) clusters at the same (dow, hour) + one short.
            "down_clusters": [
                {"duration_minutes": 150, "start_dow": 2, "start_hour": 9},
                {"duration_minutes": 150, "start_dow": 2, "start_hour": 9},
                {"duration_minutes": 30, "start_dow": 3, "start_hour": 14},
            ],
            "ll_drops_total": 120,
            "ll_active_days": 10,
            "ll_drops_per_active_day": 12,
            "ll_typical_shift_mins": 15,
            "ll_drop_hours": [{"hour": 11, "count": 40}, {"hour": 9, "count": 10}],
            "ll_drop_dow": [{"dow": 1, "count": 30}],
        },
        {
            "ride_name": "Pirates of the Caribbean",
            "ride_id": "potc",
            "park_key": "magic_kingdom",
            "downtime_pct": 1.0,
            "avg_wait": 20,
            "max_wait": 40,
            "total_polls": 900,
            "dow_hourly": [],
            "down_clusters": [],
            # no ll_drops_total → LL data unavailable
        },
        {
            "ride_name": "Test Track",
            "ride_id": "tt",
            "park_key": "epcot",
            "downtime_pct": 12.0,
            "avg_wait": 60,
            "max_wait": 120,
            "total_polls": 800,
        },
    ],
}

BASELINES_FIXTURE = {"rides": {"sm": {"10": 15, "11": 20}}}


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Each test starts with empty caches + a configured bucket name."""
    monkeypatch.setattr(server_http, "_snapshot_cache", None)
    monkeypatch.setattr(server_http, "_baselines_cache", None)
    monkeypatch.setattr(server_http, "_SNAPSHOT_BUCKET", "test-bucket")
    yield


@pytest.fixture
def stub_snapshot(monkeypatch):
    """Stub the S3 loaders so pure-function tool tests don't touch AWS."""
    monkeypatch.setattr(server_http, "_snapshot", lambda: SNAPSHOT_FIXTURE)
    monkeypatch.setattr(server_http, "_baselines", lambda: BASELINES_FIXTURE)


def _s3_returning(payload: dict):
    """Build a mock boto3 s3 client whose get_object returns `payload`."""
    body = io.BytesIO(json.dumps(payload).encode())
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": body}
    return s3


# ─── S3 loader + cache ──────────────────────────────────────────────


class TestSnapshotLoader:
    def test_fetches_from_s3_and_caches(self):
        s3 = _s3_returning(SNAPSHOT_FIXTURE)
        with patch("boto3.client", return_value=s3) as mk:
            first = server_http._snapshot()
            second = server_http._snapshot()
        assert first == SNAPSHOT_FIXTURE
        assert second is first  # cached object, not re-parsed
        # boto3.client + get_object each called exactly once despite two reads
        assert mk.call_count == 1
        assert s3.get_object.call_count == 1

    def test_unconfigured_bucket_raises_unavailable(self, monkeypatch):
        monkeypatch.setattr(server_http, "_SNAPSHOT_BUCKET", "")
        with pytest.raises(server_http._SnapshotUnavailable):
            server_http._snapshot()

    def test_s3_error_raises_unavailable(self):
        s3 = MagicMock()
        s3.get_object.side_effect = RuntimeError("connection reset")
        with patch("boto3.client", return_value=s3):
            with pytest.raises(server_http._SnapshotUnavailable):
                server_http._snapshot()

    def test_baselines_uses_baselines_key(self):
        s3 = _s3_returning(BASELINES_FIXTURE)
        with patch("boto3.client", return_value=s3):
            assert server_http._baselines() == BASELINES_FIXTURE
        # fetched against the baselines key, not the snapshot key
        _, kwargs = s3.get_object.call_args
        assert kwargs["Key"] == server_http._BASELINES_KEY


# ─── Graceful degradation when the snapshot is unavailable ──────────


class TestGracefulDegradation:
    @pytest.fixture(autouse=True)
    def _snapshot_down(self, monkeypatch):
        def _boom():
            raise server_http._SnapshotUnavailable("S3 down")
        monkeypatch.setattr(server_http, "_snapshot", _boom)
        monkeypatch.setattr(server_http, "_baselines", _boom)

    @pytest.mark.parametrize("call", [
        lambda: server_http.get_park_heatmap("MK"),
        lambda: server_http.get_ride_analytics("space"),
        lambda: server_http.get_ride_dow_pattern("space"),
        lambda: server_http.get_ride_down_clusters("space"),
        lambda: server_http.get_ride_ll_drops("space"),
        lambda: server_http.get_short_wait_baseline("space"),
        lambda: server_http.find_rides_matching(),
    ])
    def test_snapshot_tools_return_unavailable_payload(self, call):
        result = call()
        assert result["error"] == "analytics temporarily unavailable"
        assert "live tools" in result["error_hint"].lower()


# ─── Pure-function snapshot tools ───────────────────────────────────


class TestParkHeatmap:
    def test_all_days(self, stub_snapshot):
        out = server_http.get_park_heatmap("MK")
        assert out["park"] == "magic_kingdom"
        assert out["cell_count"] == 2

    def test_day_filter(self, stub_snapshot):
        out = server_http.get_park_heatmap("magic_kingdom", day_of_week="monday")
        assert out["day_of_week"] == "monday"
        assert out["cell_count"] == 1
        assert out["cells"][0]["dow"] == 1  # monday

    def test_bad_day_raises(self, stub_snapshot):
        with pytest.raises(ValueError):
            server_http.get_park_heatmap("MK", day_of_week="someday")


class TestFindRidesMatching:
    def test_park_filter_and_default_sort(self, stub_snapshot):
        out = server_http.find_rides_matching(park="magic_kingdom")
        assert out["match_count"] == 2
        # default sort downtime_pct desc → Space (5.0) before Pirates (1.0)
        assert out["rides"][0]["ride_name"] == "Space Mountain"

    def test_max_downtime_filter(self, stub_snapshot):
        out = server_http.find_rides_matching(max_downtime_pct=2.0)
        names = {r["ride_name"] for r in out["rides"]}
        assert names == {"Pirates of the Caribbean"}

    def test_invalid_sort_raises(self, stub_snapshot):
        with pytest.raises(ValueError):
            server_http.find_rides_matching(sort_by="nonsense")


class TestRideAnalyticsAndClusters:
    def test_analytics_substring_match(self, stub_snapshot):
        out = server_http.get_ride_analytics("space")
        assert out["ride_name"] == "Space Mountain"

    def test_down_cluster_summary(self, stub_snapshot):
        out = server_http.get_ride_down_clusters("space")
        assert out["cluster_count"] == 3
        assert out["long_cluster_count"] == 2  # the two 150-min clusters
        assert out["total_downtime_minutes"] == 330
        assert out["most_common_start"]["dow"] == 2
        assert out["most_common_start"]["hour"] == 9
        assert out["most_common_start"]["occurrences"] == 2

    def test_no_ride_match_raises(self, stub_snapshot):
        with pytest.raises(ValueError):
            server_http.get_ride_analytics("does-not-exist")


class TestLLDrops:
    def test_ride_with_drops(self, stub_snapshot):
        out = server_http.get_ride_ll_drops("space")
        assert out["data_available"] is True
        assert out["total_drops"] == 120
        # top_drop_hours sorted by count desc
        assert out["top_drop_hours"][0] == {"hour": 11, "count": 40}

    def test_ride_without_drops(self, stub_snapshot):
        out = server_http.get_ride_ll_drops("pirates")
        assert out["data_available"] is False


class TestShortWaitBaseline:
    def test_single_hour(self, stub_snapshot):
        out = server_http.get_short_wait_baseline("space", hour=10)
        assert out["threshold_minutes"] == 15

    def test_all_hours(self, stub_snapshot):
        out = server_http.get_short_wait_baseline("space")
        assert out["thresholds_by_hour"] == {10: 15, 11: 20}

    def test_hour_out_of_range_raises(self, stub_snapshot):
        with pytest.raises(ValueError):
            server_http.get_short_wait_baseline("space", hour=99)


class TestDowPattern:
    def test_day_filter(self, stub_snapshot):
        out = server_http.get_ride_dow_pattern("space", day_of_week="sunday")
        assert out["day_of_week"] == "sunday"
        assert out["cell_count"] == 1  # the one dow=0 cell


# ─── Bundled-data tools ─────────────────────────────────────────────


class TestBundledDataTools:
    def test_mll_tiers_missing_file(self, monkeypatch):
        monkeypatch.setattr(server_http, "_mll_tiers", lambda: {})
        out = server_http.get_mll_tiers("MK")
        assert "missing" in out["error"].lower()

    def test_mll_tiers_happy(self, monkeypatch):
        monkeypatch.setattr(server_http, "_mll_tiers", lambda: {
            "updated_at": "2026-05-01",
            "magic_kingdom": {"has_tiers": True, "tier_1": ["Space Mountain"], "tier_2": []},
        })
        out = server_http.get_mll_tiers("magic_kingdom")
        assert out["has_tiers"] is True
        assert out["tier_1"] == ["Space Mountain"]

    def test_party_calendar_missing_file(self, monkeypatch):
        monkeypatch.setattr(server_http, "_party_calendar", lambda: {})
        out = server_http.get_party_calendar()
        assert "missing" in out["error"].lower()

    def test_party_calendar_match(self, monkeypatch):
        monkeypatch.setattr(server_http, "_party_calendar", lambda: {
            "updated_at": "2026-05-01",
            "parties": {
                "MVMCP": {
                    "full_name": "Mickey's Very Merry Christmas Party",
                    "park": "magic_kingdom",
                    "dates": ["2026-11-08", "2026-11-10"],
                    "park_closes_early_for_non_party": True,
                },
            },
        })
        out = server_http.get_party_calendar(date="2026-11-08", days_ahead=7)
        assert len(out["parties"]) == 1
        p = out["parties"][0]
        assert p["abbreviation"] == "MVMCP"
        assert p["is_party_day_on_target_date"] is True
        assert "2026-11-10" in p["dates_in_range"]


# ─── Live DDB tools: validation + resolution-failure paths ──────────


class TestLiveToolValidation:
    def test_downtime_negative_days_raises(self):
        with pytest.raises(ValueError):
            server_http.get_ride_downtime_today("space", days_back=-1)

    def test_downtime_beyond_retention_raises(self):
        with pytest.raises(ValueError):
            server_http.get_ride_downtime_today("space", days_back=999)

    def test_forecast_no_ride_match(self, monkeypatch):
        monkeypatch.setattr(server_http, "_resolve_ride_via_ddb", lambda n: None)
        out = server_http.get_ride_forecast("nonexistent")
        assert "No ride matching" in out["error"]

    def test_downtime_no_ride_match(self, monkeypatch):
        monkeypatch.setattr(server_http, "_resolve_ride_via_ddb", lambda n: None)
        out = server_http.get_ride_downtime_today("nonexistent")
        assert "No ride matching" in out["error"]


# ─── Showtime classifier (verbatim port — sanity that it ported) ────


class TestClassifyShow:
    @pytest.mark.parametrize("name,expected", [
        ("Happily Ever After", "spectacular"),
        ("Festival of Fantasy Parade", "parade"),
        ("Festival of the Lion King", "stage"),
        ("Dapper Dans", "music"),
        ("Meet Mickey Mouse", "character_meet"),
        ("Some Random Juggler", "atmosphere"),
        ("Indiana Jones Epic Stunt Spectacular", "stage"),  # override beats SPECTACULAR_RX
    ])
    def test_buckets(self, name, expected):
        assert server_http._classify_show(name) == expected
