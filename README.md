# rolling-cost-cap

[![CI](https://github.com/coreystevensdev/rolling-cost-cap/actions/workflows/ci.yml/badge.svg)](https://github.com/coreystevensdev/rolling-cost-cap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rolling-cost-cap)](https://pypi.org/project/rolling-cost-cap/)

A runaway-cost circuit breaker for LLM and metered API calls. Judges every observed cost against three independent layers: a rolling-median anomaly cap, an absolute per-call ceiling, and a cumulative monthly budget. Zero dependencies, thread-safe, fully typed, 27 tests.

```bash
pip install rolling-cost-cap
```

## Problem

A single malformed request can make an LLM call cost 50x the usual amount: a prompt that triggers maximum-length output, a retry loop that compounds, a model misconfiguration that routes traffic to a tier 5x more expensive. Provider billing alerts fire hours later, and a fixed per-call dollar limit is either too loose to catch anomalies or too tight for legitimate heavy calls.

The core difficulty is that "expensive" is relative. $0.10 is an anomaly when your median call costs $0.02, and completely normal when it costs $0.08. A static threshold cannot express that.

## Solution

`CostCap` keeps a rolling window of recent call costs and trips when a new cost exceeds a multiple of the window's median. The median self-calibrates to your real traffic, so the same configuration works whether your calls cost fractions of a cent or dollars. Two fixed layers back it up: an absolute per-call ceiling that catches runaways before the median has warmed up, and a monthly budget that bounds total damage.

Decision precedence: absolute ceiling, then monthly budget, then rolling median. The median layer stays dormant until `min_samples` costs have been recorded, so one cheap first call cannot set a hair-trigger baseline. A median of zero (free calls) also keeps it dormant rather than tripping on the first nonzero cost.

**Implementation:** The rolling window is a fixed-size `collections.deque`. The median is computed via `statistics.median()` on a snapshot of the deque each evaluation, which is O(n log n). At realistic API-call rates this is noise; the snapshot approach was chosen over a two-heap incremental median because it is simpler to verify and the window is small. A `threading.Lock` guards all reads and writes so `evaluate` and `check` are safe to call from multiple threads or concurrent coroutines.

This design was extracted from the cost-control layer of [InvoiceFlow](https://github.com/coreystevensdev/invoiceflow), where it runs in production as a post-call ceiling on Anthropic API spend.

## Usage

```python
from rolling_cost_cap import CostCap

cap = CostCap(
    multiplier=3.0,        # trip when cost > 3x rolling median
    window=50,             # median over the last 50 calls
    min_samples=5,         # median layer dormant until 5 costs recorded
    absolute_ceiling=1.00, # never allow a single call over $1
    monthly_budget=25.00,  # stop everything past $25/month
)

# After each API call, compute the observed cost and evaluate it
usage = response.usage
cost = (usage.input_tokens / 1e6) * 3.00 + (usage.output_tokens / 1e6) * 15.00

decision = cap.evaluate(cost)
if not decision.allowed:
    match decision.trip:
        case "absolute-ceiling":
            alert(f"single call cost ${decision.observed:.2f}")
        case "rolling-median":
            alert(f"cost {decision.observed / decision.median:.0f}x the median")
        case "monthly-budget":
            disable_feature_until_next_month()
```

`evaluate` judges the cost against the history that preceded it, then records it. Recording happens even when the decision trips, because the money is already spent and hiding real spend would make the monthly total under-count. For pre-call gating with an estimate, use `check`, which never records:

```python
if not cap.check(estimated_cost).allowed:
    return err_budget_exceeded()
```

The monthly window follows the wall clock (UTC by default) and resets on calendar-month rollover. Inject a `clock` callable to control time in tests:

```python
cap = CostCap(monthly_budget=25.0, clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc))
```

## API

| Member | Behavior |
|---|---|
| `evaluate(cost) -> Decision` | Judge against prior history, then record. The common post-call path. |
| `check(cost) -> Decision` | Judge without recording. For pre-call estimates. |
| `record(cost) -> None` | Record without judging. For backfilling history. |
| `median` | Current rolling median, or `None` with empty history. |
| `monthly_spend` | Cumulative recorded spend this calendar month. |
| `reset()` | Clear window and monthly state. |

`Decision` is a frozen dataclass: `allowed`, `trip` (`"absolute-ceiling" | "monthly-budget" | "rolling-median" | None`), `observed`, `cap`, `median`, `monthly_spend`. All fields reflect state at decision time, before the cost is recorded.

Costs must be non-negative finite numbers; `NaN`, infinity, negatives, and non-numeric types raise instead of being silently ignored.

## Benchmarks

`evaluate` throughput on Apple Silicon (M-series), CPython 3.14, single thread, via `python bench.py`:

| Window | Throughput | Latency |
|---|---|---|
| 50 (default) | 72,800 ops/sec | 13.7 us |
| 200 | 45,400 ops/sec | 22.0 us |
| 1,000 | 10,700 ops/sec | 93.0 us |

The median is recomputed from a window snapshot on every decision, which is O(n log n). At realistic API-call rates (tens of calls per second) this is noise; the naive recompute was chosen over an incremental two-heap median because it is simpler to verify and the window is small.

## Known limitations

- **State is in-process.** Each worker, container, or serverless instance caps independently. A fleet of N workers can collectively spend up to N times the monthly budget. Acceptable as an anomaly detector and defense-in-depth; not a substitute for a provider-side billing cap.
- **The rolling median can drift under a slow ramp.** Costs that grow gradually (each within `multiplier` of the current median) raise the baseline without ever tripping. The absolute ceiling is the backstop for this case.
- **`evaluate` is post-call.** It detects that money was spent; it does not prevent the spend. Real prevention lives upstream: bounded retries, `max_tokens` limits, and `check` with estimates where estimation is feasible.
- **Monthly rollover is calendar-based and UTC by default.** There is no proration and no billing-cycle alignment beyond what a custom `clock` gives you.
- **No persistence.** A process restart forgets both the window and the month-to-date spend.
