"""
EWMA initialization and history-length experiment for Project Trade Group A.

Run from Project Trade root with venv active:
    python .\test_group_a_ewma_init_sweep.py

Read-only DB access. Does not modify model code, DB, or live settings.
Uses historical-source NIFTY50 OHLC, next-30-return target, 30-minute sampling.
All variants are evaluated at the same timestamps, with up to 180 prior closes
available. The chronological 70/30 split is rebuilt on these common timestamps.
Lambda is fixed at 0.94.

Selection: development MAE only. Holdout is for final confirmation.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
INDEX_ID = "NIFTY50"
BASE_LOOKBACK = 60
MAX_LOOKBACK = 180
HORIZON = 30
SAMPLE_EVERY = 30
ANNUALIZATION = math.sqrt(252 * 375)
LAM = 0.94


def valid_ohlc(row):
    _, o, h, l, c, _volume, _source = row
    vals = [o, h, l, c]
    if not all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in vals):
        return False
    o, h, l, c = map(float, vals)
    return h >= max(o, c) and l <= min(o, c) and h >= l


def target_vol(closes):
    returns = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(returns) < 2 or not np.all(np.isfinite(returns)):
        return None
    return float(np.std(returns, ddof=1) * ANNUALIZATION * 100.0)


def ewma_from_returns(returns, method="current"):
    """Return annualized vol %. `current` exactly mirrors existing recursion."""
    returns = np.asarray(returns, dtype=float)
    if returns.ndim != 1 or returns.size < 2 or not np.all(np.isfinite(returns)):
        return None
    if method == "current":
        variance = float(returns[0] ** 2)
        update_returns = returns[1:]
    elif method == "sample_variance_init":
        # Alternative: initialize with the full window's sample variance and
        # make no extra update, avoiding double-counting the latest return.
        variance = float(np.var(returns, ddof=1))
        update_returns = []
    elif method == "mean_square_init":
        # Alternative: initialize with average squared return over the window.
        variance = float(np.mean(returns * returns))
        update_returns = []
    else:
        raise ValueError(method)
    for ret in update_returns:
        variance = LAM * variance + (1.0 - LAM) * float(ret * ret)
    value = math.sqrt(max(variance, 0.0)) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) else None


def metric(values, actual, indices):
    errors = [values[i] - actual[i] for i in indices if i in values]
    if not errors:
        return None
    return (len(errors), statistics.mean(abs(e) for e in errors),
            math.sqrt(statistics.mean(e * e for e in errors)),
            statistics.mean(values[i] for i in indices if i in values),
            statistics.mean(actual[i] for i in indices if i in values))


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}. Run from project root.")
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("""
            SELECT minute_bucket, open, high, low, close, volume, source
            FROM candles
            WHERE index_id=? AND source='historical'
            ORDER BY minute_bucket
        """, (INDEX_ID,)).fetchall()
    finally:
        con.close()
    rows = [r for r in rows if valid_ohlc(r)]

    runs, current = [], []
    prev_dt = prev_date = None
    for row in rows:
        dt = datetime.fromisoformat(row[0])
        if current and ((dt - prev_dt).total_seconds() != 60 or dt.date() != prev_date):
            runs.append(current)
            current = []
        current.append((row, dt))
        prev_dt, prev_date = dt, dt.date()
    if current:
        runs.append(current)

    cases = []
    for run in runs:
        # Require 180 prior closes so all variants are evaluated on identical timestamps.
        for i in range(MAX_LOOKBACK - 1, len(run) - HORIZON - 1, SAMPLE_EVERY):
            in_rows = [x[0] for x in run[i - MAX_LOOKBACK + 1:i + 1]]
            fut_rows = [x[0] for x in run[i + 1:i + HORIZON + 1]]
            if len(in_rows) != MAX_LOOKBACK or len(fut_rows) != HORIZON:
                continue
            all_closes = [float(r[4]) for r in in_rows]
            y = target_vol([all_closes[-1]] + [float(r[4]) for r in fut_rows])
            if y is not None and math.isfinite(y):
                cases.append((in_rows[-1][0], all_closes, y))

    if len(cases) < 100:
        raise RuntimeError(f"Only {len(cases)} cases; refusing to report a weak evaluation.")

    variants = {
        "current_60bar": ("current recursion, last 60 closes", 60, "current"),
        "current_120bar": ("current recursion, last 120 closes", 120, "current"),
        "current_180bar": ("current recursion, last 180 closes", 180, "current"),
        "samplevar_init_60": ("sample variance init, last 60 returns", 60, "sample_variance_init"),
        "meansq_init_60": ("mean-square init, last 60 returns", 60, "mean_square_init"),
    }
    split = int(len(cases) * 0.70)
    actual = {i: c[2] for i, c in enumerate(cases)}
    dev_idx = list(range(split))
    hold_idx = list(range(split, len(cases)))
    forecasts = {name: {} for name in variants}

    start = time.time()
    for i, (_timestamp, closes180, _y) in enumerate(cases):
        for name, (_description, lookback, method) in variants.items():
            closes = closes180[-lookback:]
            returns = np.diff(np.log(np.asarray(closes, dtype=float)))
            val = ewma_from_returns(returns, method)
            if val is not None:
                forecasts[name][i] = val
        if (i + 1) % 1000 == 0:
            print(f"Processed {i+1:,}/{len(cases):,} cases; elapsed {time.time()-start:.1f}s")

    common = set(actual)
    for values in forecasts.values():
        common &= set(values)
    dev_common = sorted(i for i in common if i < split)
    hold_common = sorted(i for i in common if i >= split)

    print("=" * 100)
    print("GROUP A — EWMA INITIALIZATION / HISTORY-LENGTH EXPERIMENT (READ-ONLY)")
    print("=" * 100)
    print(f"Cases: {len(cases):,} | target: next {HORIZON} one-minute returns | sample every {SAMPLE_EVERY} min")
    print(f"All variants have identical timestamps and at least {MAX_LOOKBACK} prior closes available.")
    print(f"Development: {cases[0][0]} through {cases[split-1][0]} ({split:,} cases)")
    print(f"Holdout:     {cases[split][0]} through {cases[-1][0]} ({len(cases)-split:,} cases)")
    print(f"Common cases: development {len(dev_common):,}; holdout {len(hold_common):,}")
    print(f"Lambda fixed at {LAM:.2f}; live code unchanged.")
    print("\nLower MAE/RMSE is better.")
    print(f"{'Variant':22s} {'Dev MAE':>10s} {'Dev RMSE':>10s} {'Holdout MAE':>13s} {'Holdout RMSE':>14s}")
    print("-" * 76)
    results = []
    for name, (description, _lookback, _method) in variants.items():
        dev = metric(forecasts[name], actual, dev_common)
        hold = metric(forecasts[name], actual, hold_common)
        if dev and hold:
            results.append((name, dev, hold, description))
            print(f"{name:22s} {dev[1]:10.4f} {dev[2]:10.4f} {hold[1]:13.4f} {hold[2]:14.4f}")
    if results:
        best = min(results, key=lambda x: x[1][1])
        print(f"\nDevelopment-MAE winner: {best[0]} — {best[3]}")
        print("Use holdout only as a final check; do not choose a variant from holdout metrics.")
    print("\nCaution: 60/120/180 bars change the estimator's information window; initialization alternatives use 60 returns.")
    print("Target windows overlap, so these metrics are descriptive, not independent-sample significance tests.")
    print("No project code, database, or live settings were modified.")


if __name__ == "__main__":
    main()
