"""Plan-drift calculator (index._compute_plan_drift)."""
import index


def _rides(*preds):
    return [{"ride_id": f"r{i}", "predicted_wait_min": p} for i, p in enumerate(preds)]


class TestComputePlanDrift:
    def test_lighter_than_planned_positive(self):
        rides = _rides(60, 40)  # predicted 60, 40
        waits = {"r0": 20, "r1": 15}  # current much lower
        net, n = index._compute_plan_drift(rides, waits)
        assert net == (60 - 20) + (40 - 15) == 65
        assert n == 2

    def test_heavier_than_planned_negative(self):
        rides = _rides(20, 15)
        waits = {"r0": 60, "r1": 50}
        net, n = index._compute_plan_drift(rides, waits)
        assert net == (20 - 60) + (15 - 50)  # -75
        assert n == 2

    def test_skips_rides_without_prediction_or_wait(self):
        rides = [
            {"ride_id": "a", "predicted_wait_min": 40},   # has both
            {"ride_id": "b", "predicted_wait_min": None}, # no prediction
            {"ride_id": "c", "predicted_wait_min": 30},   # no current wait
        ]
        waits = {"a": 10, "b": 5}  # c missing
        net, n = index._compute_plan_drift(rides, waits)
        assert net == 30 and n == 1  # only ride a compared
