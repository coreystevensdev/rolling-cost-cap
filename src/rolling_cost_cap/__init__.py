"""Runaway-cost circuit breaker for metered API calls.

Judges each observed cost against three independent layers: an optional
absolute per-call ceiling, an optional cumulative monthly budget, and a
rolling-median anomaly cap. State is in-process only; if you run
multiple workers, each worker caps independently (see README).
"""

from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

__all__ = ["CostCap", "Decision", "Trip"]

Trip = Literal["absolute-ceiling", "monthly-budget", "rolling-median"]


@dataclass(frozen=True)
class Decision:
    """Outcome of judging one cost. Reflects state before the cost is recorded."""

    allowed: bool
    trip: Trip | None
    observed: float
    cap: float | None
    median: float | None
    monthly_spend: float


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CostCap:
    """Three-layer cost circuit breaker.

    The rolling-median layer trips when a cost exceeds ``multiplier`` times
    the median of the last ``window`` recorded costs. It stays dormant until
    ``min_samples`` costs have been recorded, so one cheap first call cannot
    set a hair-trigger baseline.
    """

    def __init__(
        self,
        *,
        multiplier: float = 3.0,
        window: int = 50,
        min_samples: int = 5,
        absolute_ceiling: float | None = None,
        monthly_budget: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if window < 1:
            raise ValueError("window must be >= 1")
        if not 1 <= min_samples <= window:
            raise ValueError("min_samples must be between 1 and window")
        if absolute_ceiling is not None and absolute_ceiling <= 0:
            raise ValueError("absolute_ceiling must be positive")
        if monthly_budget is not None and monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive")

        self._multiplier = multiplier
        self._min_samples = min_samples
        self._absolute_ceiling = absolute_ceiling
        self._monthly_budget = monthly_budget
        self._clock = clock or _utc_now
        self._history: deque[float] = deque(maxlen=window)
        self._month_key = self._current_month()
        self._monthly_spend = 0.0
        self._lock = threading.Lock()

    def check(self, cost: float) -> Decision:
        self._validate(cost)
        with self._lock:
            self._roll_month()
            return self._decide(cost)

    def record(self, cost: float) -> None:
        self._validate(cost)
        with self._lock:
            self._roll_month()
            self._append(cost)

    def evaluate(self, cost: float) -> Decision:
        """Judge a cost, then record it.

        Recording happens even when the decision trips: the money is already
        spent, and hiding real spend from the monthly total would let the
        budget layer under-count. The spike is judged against the history
        that preceded it.
        """
        self._validate(cost)
        with self._lock:
            self._roll_month()
            decision = self._decide(cost)
            self._append(cost)
            return decision

    @property
    def median(self) -> float | None:
        with self._lock:
            return self._median()

    @property
    def monthly_spend(self) -> float:
        with self._lock:
            self._roll_month()
            return self._monthly_spend

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._month_key = self._current_month()
            self._monthly_spend = 0.0

    def _validate(self, cost: float) -> None:
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            raise TypeError(f"cost must be a number, got {type(cost).__name__}")
        if not math.isfinite(cost) or cost < 0:
            raise ValueError(f"cost must be a non-negative finite number, got {cost}")

    def _decide(self, cost: float) -> Decision:
        median = self._median()
        spend = self._monthly_spend

        if self._absolute_ceiling is not None and cost > self._absolute_ceiling:
            return Decision(False, "absolute-ceiling", cost, self._absolute_ceiling, median, spend)

        if self._monthly_budget is not None and spend + cost > self._monthly_budget:
            return Decision(False, "monthly-budget", cost, self._monthly_budget, median, spend)

        if median is not None and median > 0 and len(self._history) >= self._min_samples:
            cap = median * self._multiplier
            if cost > cap:
                return Decision(False, "rolling-median", cost, cap, median, spend)
            return Decision(True, None, cost, cap, median, spend)

        return Decision(True, None, cost, None, median, spend)

    def _median(self) -> float | None:
        if not self._history:
            return None
        return statistics.median(self._history)

    def _append(self, cost: float) -> None:
        self._history.append(float(cost))
        self._monthly_spend += cost

    def _current_month(self) -> str:
        return self._clock().strftime("%Y-%m")

    def _roll_month(self) -> None:
        now = self._current_month()
        if now != self._month_key:
            self._month_key = now
            self._monthly_spend = 0.0
