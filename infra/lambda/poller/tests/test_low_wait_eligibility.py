"""The Pi app's low-wait eligibility rule, extended to both baselines
(2026-07-04): only rides whose typical wait is ever long (present in
baselines.json) may fire ANY low-wait-class alert. The forecast path
alerted "Gran Fiesta 10 min!" on July 4th because holiday forecasts
inflate walk-ons — the ride itself is never worth a push."""
import index


class TestHistoricallyInteresting:
    def test_baseline_ride_is_eligible(self, monkeypatch):
        monkeypatch.setattr(index, "_LOW_WAIT_THRESHOLDS", {"space": {"14": 20}})
        assert index._historically_interesting("space")

    def test_walk_on_without_baseline_is_not(self, monkeypatch):
        monkeypatch.setattr(index, "_LOW_WAIT_THRESHOLDS", {"space": {"14": 20}})
        assert not index._historically_interesting("gran-fiesta")

    def test_empty_baselines_disable_all_low_wait(self, monkeypatch):
        monkeypatch.setattr(index, "_LOW_WAIT_THRESHOLDS", {})
        assert not index._historically_interesting("space")
