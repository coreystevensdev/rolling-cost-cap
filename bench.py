"""Throughput benchmark for CostCap.evaluate at several window sizes.

Run: PYTHONPATH=src python bench.py   (or `pip install -e .` first)

Reports the best of several timed repeats after a warm-up pass. A single
un-warmed pass, which is what this did originally, spread about 2x run to run
on the same machine and produced a number with no business carrying three
significant figures.
"""

import platform
import random
import sys
import timeit

from rolling_cost_cap import CostCap

ITERATIONS = 200_000
REPEATS = 5


def bench(window: int) -> float:
    """Best-of-REPEATS throughput, in evaluate calls per second."""
    rng = random.Random(42)
    costs = [rng.uniform(0.01, 0.05) for _ in range(ITERATIONS)]

    def run() -> None:
        # A fresh cap per repeat: the rolling window fills during the first few
        # hundred calls, so reusing one would time a different code path on
        # every repeat but the first.
        cap = CostCap(window=window, absolute_ceiling=10.0, monthly_budget=1e9)
        for cost in costs:
            cap.evaluate(cost)

    # Best rather than mean: the slow runs are measuring the machine's other
    # work, not this code.
    best = min(timeit.repeat(run, repeat=REPEATS, number=1))
    return ITERATIONS / best


if __name__ == "__main__":
    print(f"{platform.platform()}, Python {sys.version.split()[0]}")
    print(f"best of {REPEATS} repeats, {ITERATIONS:,} calls each\n")
    for window in (50, 200, 1000):
        ops = bench(window)
        print(f"window={window:>5}  {ops:>12,.0f} evaluate/sec  ({1e6 / ops:.1f} us/op)")
