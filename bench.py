"""Throughput benchmark for CostCap.evaluate at several window sizes.

Run: python bench.py
"""

import random
import time

from rolling_cost_cap import CostCap

ITERATIONS = 200_000


def bench(window: int) -> float:
    cap = CostCap(window=window, absolute_ceiling=10.0, monthly_budget=1e9)
    rng = random.Random(42)
    costs = [rng.uniform(0.01, 0.05) for _ in range(ITERATIONS)]
    start = time.perf_counter()
    for cost in costs:
        cap.evaluate(cost)
    elapsed = time.perf_counter() - start
    return ITERATIONS / elapsed


if __name__ == "__main__":
    for window in (50, 200, 1000):
        ops = bench(window)
        print(f"window={window:>5}  {ops:>12,.0f} evaluate/sec  ({1e6 / ops:.1f} us/op)")
