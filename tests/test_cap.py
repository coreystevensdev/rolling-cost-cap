import threading
from datetime import datetime, timezone

import pytest

from rolling_cost_cap import CostCap


def utc(year, month, day=1):
    return datetime(year, month, day, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class TestConstructorValidation:
    def test_multiplier_below_one_rejected(self):
        with pytest.raises(ValueError, match="multiplier"):
            CostCap(multiplier=0.9)

    def test_window_below_one_rejected(self):
        with pytest.raises(ValueError, match="window"):
            CostCap(window=0)

    def test_min_samples_above_window_rejected(self):
        with pytest.raises(ValueError, match="min_samples"):
            CostCap(window=10, min_samples=11)

    def test_zero_ceiling_rejected(self):
        with pytest.raises(ValueError, match="absolute_ceiling"):
            CostCap(absolute_ceiling=0)

    def test_negative_budget_rejected(self):
        with pytest.raises(ValueError, match="monthly_budget"):
            CostCap(monthly_budget=-5)


class TestCostValidation:
    def test_negative_cost_raises(self):
        with pytest.raises(ValueError):
            CostCap().evaluate(-0.01)

    def test_nan_raises(self):
        with pytest.raises(ValueError):
            CostCap().evaluate(float("nan"))

    def test_infinity_raises(self):
        with pytest.raises(ValueError):
            CostCap().evaluate(float("inf"))

    def test_bool_raises_type_error(self):
        with pytest.raises(TypeError):
            CostCap().evaluate(True)

    def test_string_raises_type_error(self):
        with pytest.raises(TypeError):
            CostCap().evaluate("0.02")


class TestAbsoluteCeiling:
    def test_trips_above_ceiling(self):
        cap = CostCap(absolute_ceiling=1.0)
        decision = cap.evaluate(1.5)
        assert not decision.allowed
        assert decision.trip == "absolute-ceiling"
        assert decision.cap == 1.0

    def test_allows_at_ceiling(self):
        cap = CostCap(absolute_ceiling=1.0)
        assert cap.evaluate(1.0).allowed

    def test_no_ceiling_never_trips_on_first_call(self):
        assert CostCap().evaluate(500.0).allowed


class TestRollingMedian:
    def test_dormant_below_min_samples(self):
        cap = CostCap(min_samples=5)
        for _ in range(4):
            cap.evaluate(0.02)
        assert cap.evaluate(10.0).allowed

    def test_trips_after_warmup(self):
        cap = CostCap(multiplier=3.0, min_samples=5)
        for _ in range(5):
            cap.evaluate(0.02)
        decision = cap.evaluate(0.10)
        assert not decision.allowed
        assert decision.trip == "rolling-median"
        assert decision.median == pytest.approx(0.02)
        assert decision.cap == pytest.approx(0.06)

    def test_allows_within_multiplier(self):
        cap = CostCap(multiplier=3.0, min_samples=5)
        for _ in range(5):
            cap.evaluate(0.02)
        assert cap.evaluate(0.05).allowed

    def test_zero_median_stays_dormant(self):
        cap = CostCap(min_samples=1)
        for _ in range(5):
            cap.evaluate(0.0)
        assert cap.evaluate(0.5).allowed

    def test_spike_judged_against_prior_history(self):
        cap = CostCap(multiplier=3.0, min_samples=5)
        for _ in range(10):
            cap.evaluate(0.02)
        assert not cap.evaluate(0.10).allowed
        # the spike entered the window but ten 0.02s still dominate the median
        assert not cap.evaluate(0.10).allowed

    def test_window_eviction_forgets_old_costs(self):
        cap = CostCap(window=5, min_samples=5, multiplier=3.0)
        for _ in range(5):
            cap.evaluate(1.0)
        for _ in range(5):
            cap.evaluate(0.01)
        # window now holds only 0.01s, so 1.0 is an anomaly again
        assert not cap.evaluate(1.0).allowed

    def test_median_averages_middle_pair_on_even_window(self):
        cap = CostCap(min_samples=1)
        for cost in (0.01, 0.02, 0.03, 0.04):
            cap.evaluate(cost)
        assert cap.median == pytest.approx(0.025)

    def test_median_averages_middle_pair_on_two_sample_window(self):
        cap = CostCap(min_samples=1)
        cap.evaluate(0.01)
        cap.evaluate(0.05)
        assert cap.median == pytest.approx(0.03)


class TestMonthlyBudget:
    def test_trips_when_cost_would_exceed_budget(self):
        cap = CostCap(monthly_budget=1.0)
        cap.evaluate(0.6)
        decision = cap.evaluate(0.5)
        assert not decision.allowed
        assert decision.trip == "monthly-budget"
        assert decision.monthly_spend == pytest.approx(0.6)

    def test_spend_recorded_even_when_tripped(self):
        cap = CostCap(monthly_budget=1.0)
        cap.evaluate(0.6)
        cap.evaluate(0.5)
        assert cap.monthly_spend == pytest.approx(1.1)

    def test_resets_on_month_rollover(self):
        clock = FakeClock(utc(2026, 7))
        cap = CostCap(monthly_budget=1.0, clock=clock)
        cap.evaluate(0.9)
        assert not cap.check(0.2).allowed
        clock.now = utc(2026, 8)
        assert cap.check(0.2).allowed
        assert cap.monthly_spend == 0.0

    def test_rolling_window_survives_month_rollover(self):
        clock = FakeClock(utc(2026, 7))
        cap = CostCap(multiplier=3.0, min_samples=5, clock=clock)
        for _ in range(5):
            cap.evaluate(0.02)
        clock.now = utc(2026, 8)
        assert not cap.evaluate(0.10).allowed


class TestCheckVersusEvaluate:
    def test_check_does_not_record(self):
        cap = CostCap()
        cap.check(0.5)
        assert cap.monthly_spend == 0.0
        assert cap.median is None

    def test_record_without_decision(self):
        cap = CostCap()
        cap.record(0.5)
        assert cap.monthly_spend == pytest.approx(0.5)
        assert cap.median == pytest.approx(0.5)

    def test_reset_clears_all_state(self):
        cap = CostCap()
        cap.evaluate(0.5)
        cap.reset()
        assert cap.monthly_spend == 0.0
        assert cap.median is None


class TestThreadSafety:
    def test_concurrent_evaluate_counts_all_spend(self):
        cap = CostCap()
        per_thread, threads = 200, 8

        def worker():
            for _ in range(per_thread):
                cap.evaluate(0.01)

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        assert cap.monthly_spend == pytest.approx(per_thread * threads * 0.01)
