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

    def test_skips_held_ll_rides(self):
        # r0 is held via Lightning Lane: its 90-min standby vs a 15-min
        # LL-baked prediction is exactly the false "busier than planned"
        # signal that got plan-drift gated off — it must not count.
        rides = _rides(15, 40)
        waits = {"r0": 90, "r1": 35}
        holds = {"r0": "2026-07-03T15:00:00-04:00"}
        net, n = index._compute_plan_drift(rides, waits, holds)
        assert net == 40 - 35 == 5
        assert n == 1  # only the standby ride compared

    def test_all_rides_held_yields_zero_compared(self):
        # Every remaining ride held → nothing comparable; the caller's
        # n < 2 guard keeps the alert quiet.
        rides = _rides(15, 20)
        waits = {"r0": 90, "r1": 80}
        holds = {"r0": "x", "r1": "y"}
        net, n = index._compute_plan_drift(rides, waits, holds)
        assert (net, n) == (0, 0)

    def test_no_holds_arg_matches_legacy_behavior(self):
        rides = _rides(60, 40)
        waits = {"r0": 20, "r1": 15}
        assert index._compute_plan_drift(rides, waits) == \
            index._compute_plan_drift(rides, waits, {})
