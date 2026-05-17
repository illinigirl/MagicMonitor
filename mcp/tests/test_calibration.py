"""
Tests for `_compute_calibration_summary` in mcp/server.py.

This is the server-side aggregation that powers the cross-session
agentic feedback loop — the LLM reads pre-computed numbers + ready-
made interpretation strings rather than eyeballing raw plan rows.
Same "data plane does the math, LLM narrates the lesson" pattern
that `_compute_load_vs_forecast` uses.

Tests verify:
  - Aggregation math is correct (averages, sample counts)
  - Confidence labels respect sample-size thresholds (5/3/<3 → high/medium/low)
  - Interpretation strings are populated and reflect the data direction
  - None returned when no outcomes are recorded
  - Both calibration paths exercise correctly:
      Path 1 — completed_rides (mid-trip mark_ride_complete)
      Path 2 — per_item_feedback (end-of-day recall)
  - Per-ride bias direction (positive = ride ran longer than predicted)
"""

import server


# ── Aggression aggregation ──

class TestAggression:
    """Aggression score = -1 (too aggressive) to +1 (not aggressive enough).
    The interpretation thresholds are ±0.3 — past that, surface direction."""

    def test_too_aggressive_average_surfaces_conservative_advice(self):
        plans = [
            {"outcome_recorded": True, "aggression_rating": "too_aggressive"},
            {"outcome_recorded": True, "aggression_rating": "too_aggressive"},
            {"outcome_recorded": True, "aggression_rating": "about_right"},
        ]
        summary = server._compute_calibration_summary(plans)
        assert summary is not None
        assert summary["aggression"]["avg_score"] < -0.3
        assert "conservative" in summary["aggression"]["interpretation"].lower()

    def test_not_aggressive_enough_average_surfaces_pack_more_advice(self):
        plans = [
            {"outcome_recorded": True, "aggression_rating": "not_aggressive_enough"},
            {"outcome_recorded": True, "aggression_rating": "not_aggressive_enough"},
            {"outcome_recorded": True, "aggression_rating": "about_right"},
        ]
        summary = server._compute_calibration_summary(plans)
        assert summary["aggression"]["avg_score"] > 0.3
        assert "pack more" in summary["aggression"]["interpretation"].lower()

    def test_balanced_average_surfaces_baseline_advice(self):
        plans = [
            {"outcome_recorded": True, "aggression_rating": "about_right"},
            {"outcome_recorded": True, "aggression_rating": "about_right"},
        ]
        summary = server._compute_calibration_summary(plans)
        assert -0.3 <= summary["aggression"]["avg_score"] <= 0.3
        assert "balanced" in summary["aggression"]["interpretation"].lower()


# ── Timing aggregation ──

class TestTiming:
    def test_distribution_counts_correctly(self):
        plans = [
            {"outcome_recorded": True, "timing_rating": "ran_over"},
            {"outcome_recorded": True, "timing_rating": "extra_time", "extra_time_minutes": 30},
            {"outcome_recorded": True, "timing_rating": "extra_time", "extra_time_minutes": 60},
            {"outcome_recorded": True, "timing_rating": "on_time"},
        ]
        summary = server._compute_calibration_summary(plans)
        dist = summary["timing"]["distribution"]
        assert dist["ran_over"] == 1
        assert dist["extra_time"] == 2
        assert dist["on_time"] == 1

    def test_avg_extra_time_only_includes_extra_time_outcomes(self):
        """The 30 + 60 average across the two extra_time plans = 45."""
        plans = [
            {"outcome_recorded": True, "timing_rating": "ran_over"},
            {"outcome_recorded": True, "timing_rating": "extra_time", "extra_time_minutes": 30},
            {"outcome_recorded": True, "timing_rating": "extra_time", "extra_time_minutes": 60},
        ]
        summary = server._compute_calibration_summary(plans)
        assert summary["timing"]["avg_extra_time_minutes"] == 45.0


# ── Per-ride bias (Path 1: completed_rides) ──

class TestPerRideBiasPath1Completed:
    """Mid-trip mark_ride_complete is the strongest signal — predicted
    and actual on the same entry, captured within minutes of riding."""

    def test_positive_bias_means_ride_ran_longer_than_predicted(self):
        """Big Thunder predicted 45, actual 60 → +15 bias."""
        plans = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Big Thunder",
                 "predicted_wait_min": 45,
                 "actual_wait_min": 60},
            ],
        }]
        summary = server._compute_calibration_summary(plans)
        biases = summary["per_ride_prediction_bias"]
        bt = next((b for b in biases if b["ride_name"] == "Big Thunder"), None)
        assert bt is not None
        assert bt["avg_delta_min"] == 15

    def test_negative_bias_means_ride_ran_shorter(self):
        plans = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Pirates",
                 "predicted_wait_min": 30,
                 "actual_wait_min": 15},
            ],
        }]
        summary = server._compute_calibration_summary(plans)
        biases = summary["per_ride_prediction_bias"]
        pirates = next((b for b in biases if b["ride_name"] == "Pirates"), None)
        assert pirates is not None
        assert pirates["avg_delta_min"] == -15

    def test_confidence_label_at_sample_size_boundaries(self):
        """Confidence threshold: ≥5 → high, 3-4 → medium, <3 → low.
        Documented in _BIAS_CONFIDENCE_HIGH / _MEDIUM constants."""
        # 5 samples → high
        plans_5 = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Big Thunder",
                 "predicted_wait_min": 30,
                 "actual_wait_min": 45},
            ] * 5,
        }]
        summary_5 = server._compute_calibration_summary(plans_5)
        bt_5 = summary_5["per_ride_prediction_bias"][0]
        assert bt_5["confidence"] == "high"

        # 3 samples → medium
        plans_3 = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Big Thunder",
                 "predicted_wait_min": 30,
                 "actual_wait_min": 45},
            ] * 3,
        }]
        summary_3 = server._compute_calibration_summary(plans_3)
        bt_3 = summary_3["per_ride_prediction_bias"][0]
        assert bt_3["confidence"] == "medium"

        # 2 samples → low (filtered out OR labeled low — verify either way)
        plans_2 = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Big Thunder",
                 "predicted_wait_min": 30,
                 "actual_wait_min": 45},
            ] * 2,
        }]
        summary_2 = server._compute_calibration_summary(plans_2)
        # Either omitted from the bias list, or present with confidence=low
        bt_2_entries = [b for b in summary_2["per_ride_prediction_bias"]
                        if b["ride_name"] == "Big Thunder"]
        if bt_2_entries:
            assert bt_2_entries[0]["confidence"] == "low"


# ── Per-ride bias (Path 2: per_item_feedback recall) ──

class TestPerRideBiasPath2Feedback:
    """End-of-day recall path: per_item_feedback keyed by ride_name
    with actual_wait_min; predictions live in ride_sequence."""

    def test_feedback_path_picks_up_predictions_from_ride_sequence(self):
        plans = [{
            "outcome_recorded": True,
            "ride_sequence": [
                {"ride_name": "Space Mountain", "predicted_wait_min": 50},
            ],
            "per_item_feedback": {
                "Space Mountain": {"actual_wait_min": 75},
            },
        }]
        summary = server._compute_calibration_summary(plans)
        biases = summary["per_ride_prediction_bias"]
        sm = next((b for b in biases if b["ride_name"] == "Space Mountain"), None)
        assert sm is not None
        assert sm["avg_delta_min"] == 25


# ── Edge cases ──

class TestEdgeCases:
    def test_returns_none_when_no_outcomes_recorded(self):
        """A pending plan with outcome_recorded=false should not
        contribute to the summary."""
        plans = [
            {"outcome_recorded": False, "aggression_rating": "too_aggressive"},
        ]
        assert server._compute_calibration_summary(plans) is None

    def test_returns_none_for_empty_list(self):
        assert server._compute_calibration_summary([]) is None

    def test_handles_missing_predicted_or_actual_gracefully(self):
        """If a completed ride is missing either field, it shouldn't
        crash or contribute spurious data."""
        plans = [{
            "outcome_recorded": True,
            "completed_rides": [
                {"ride_name": "Pirates", "predicted_wait_min": 20},  # no actual
                {"ride_name": "Mansion", "actual_wait_min": 35},     # no predicted
                {"ride_name": "BTM",
                 "predicted_wait_min": 40,
                 "actual_wait_min": 55},  # complete
            ],
        }]
        summary = server._compute_calibration_summary(plans)
        # Only BTM contributes a bias.
        biases = summary["per_ride_prediction_bias"] or []
        btm = next((b for b in biases if b["ride_name"] == "BTM"), None)
        assert btm is not None
        assert btm["avg_delta_min"] == 15
