"""
Corrected EWMA initialization test for Project Trade Group A.

Run from Project Trade root with venv active:
    python .\test_group_a_ewma_init_corrected.py

Read-only database access. No model code, DB, or live settings are changed.
Matches test_group_a_holdout.py: 60 candle closes (59 log returns), next-30-return
target, 30-minute sampling, chronological 70/30 split. Lambda fixed at 0.94.

EWMA variants:
- current_first_return: exact current model recursion over 59 returns
- init_first_10: initialize with mean squared first 10 returns, then update using
  returns 11..59
- init_first_20: initialize with mean squared first 20 returns, then update using
  returns 21..59
- mean_square_all_reference: simple mean squared return across all 59 returns;
  this is a reference estimator, not EWMA.

Select among genuine EWMA variants by development MAE only. Holdout is confirmation.
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
LOOKBACK_CLOSES = 60
EXPECTED_RETURNS = LOOKBACK_CLOSES - 1
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


def current_forecast(closes):
    """Mirror the current EwmaVolatility.compute recursion exactly."""
    closes = np.asarray(closes, dtype=float)
    if closes.ndim != 1 or closes.size != LOOKBACK_CLOSES:
        return None
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        return None
    returns = np.diff(np.log(closes))
    if returns.size != EXPECTED_RETURNS or not np.all(np.isfinite(returns)):
        return None
    variance = float(returns[0] ** 2)
    for ret in returns[1:]:
        variance = LAM * variance + (1.0 - LAM) * float(ret * ret)
    value = math.sqrt(max(variance, 0.0)) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) and value >= 0 else None


def initialized_ewma_forecast(closes, init_n):
    """Initialize from mean squared first init_n returns; EWMA-update the rest."""
    closes = np.asarray(closes, dtype=float)
    if closes.ndim != 1 or closes.size != LOOKBACK_CLOSES:
        return None
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        return None
    returns = np.diff(np.log(closes))
    if returns.size != EXPECTED_RETURNS or not np.all(np.isfinite(returns)):
        return None
    if init_n < 1 or init_n >= returns.size:
        return None

    variance = float(np.mean(returns[:init_n] ** 2))
    for ret in returns[init_n:]:
        variance = LAM * variance + (1.0 - LAM) * float(ret * ret)
    value = math.sqrt(max(variance, 0.0)) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) and value >= 0 else None


def metric(values, actual, indices):
    errors = [values[i] - actual[i] for i in indices if i in values]
    if not errors:
        return None
    return (
        len(errors),
        statistics.mean(abs(e) for e in errors),
        math.sqrt(statistics.mean(e * e for e in errors)),
    )


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
        for i in range(LOOKBACK_CLOSES - 1, len(run) - HORIZON - 1, SAMPLE_EVERY):
            in_rows = [x[0] for x in run[i - LOOKBACK_CLOSES + 1:i + 1]]
            fut_rows = [x[0] for x in run[i + 1:i + HORIZON + 1]]
            if len(in_rows) != LOOKBACK_CLOSES or len(fut_rows) != HORIZON:
                continue
            closes = [float(r[4]) for r in in_rows]
            y = target_vol([closes[-1]] + [float(r[4]) for r in fut_rows])
            if y is not None and math.isfinite(y):
                cases.append((in_rows[-1][0], closes, y))

    if len(cases) < 100:
        raise RuntimeError(f"Only {len(cases)} cases; refusing to report a weak evaluation.")

    split = int(len(cases) * 0.70)
    actual = {i: c[2] for i, c in enumerate(cases)}
    forecasts = {
        "current_first_return": {},
        "init_first_10": {},
        "init_first_20": {},
        "mean_square_all_reference": {},
    }

    start = time.time()
    for i, (_timestamp, closes, _y) in enumerate(cases):
        forecasts["current_first_return"][i] = current_forecast(closes)
        forecasts["init_first_10"][i] = initialized_ewma_forecast(closes, 10)
        forecasts["init_first_20"][i] = initialized_ewma_forecast(closes, 20)
        returns = np.diff(np.log(np.asarray(closes, dtype=float)))
        forecasts["mean_square_all_reference"][i] = (
            math.sqrt(float(np.mean(returns ** 2))) * ANNUALIZATION * 100.0
        )
        if (i + 1) % 1000 == 0:
            print(f"Processed {i+1:,}/{len(cases):,} cases; elapsed {time.time()-start:.1f}s")

    common = set(actual)
    for vals in forecasts.values():
        common &= {
            i for i, v in vals.items()
            if v is not None and math.isfinite(float(v)) and float(v) >= 0
        }
    dev_idx = sorted(i for i in common if i < split)
    hold_idx = sorted(i for i in common if i >= split)

    print("=" * 98)
    print("GROUP A — CORRECTED EWMA INITIALIZATION TEST (READ-ONLY)")
    print("=" * 98)
    print(f"Valid historical OHLC rows: {len(rows):,}")
    print(f"Cases: {len(cases):,} | input: {LOOKBACK_CLOSES} closes / {EXPECTED_RETURNS} returns")
    print(f"Target: next {HORIZON} one-minute returns | sampling every {SAMPLE_EVERY} min")
    print(f"Development: {cases[0][0]} through {cases[split-1][0]} ({split:,} cases)")
    print(f"Holdout:     {cases[split][0]} through {cases[-1][0]} ({len(cases)-split:,} cases)")
    print(f"Common cases: development {len(dev_idx):,}; holdout {len(hold_idx):,}")
    print(f"Lambda fixed at {LAM:.2f}; live code unchanged.")
    print("\nLower MAE/RMSE is better. Mean-square-all is a simple-variance reference, not EWMA.")
    print(f"{'Variant':30s} {'Dev MAE':>10s} {'Dev RMSE':>10s} {'Holdout MAE':>13s} {'Holdout RMSE':>14s}")
    print("-" * 82)

    results = []
    for name in forecasts:
        dev = metric(forecasts[name], actual, dev_idx)
        hold = metric(forecasts[name], actual, hold_idx)
        if dev and hold:
            results.append((name, dev, hold))
            print(f"{name:30s} {dev[1]:10.4f} {dev[2]:10.4f} {hold[1]:13.4f} {hold[2]:14.4f}")

    genuine = [r for r in results if r[0] != "mean_square_all_reference"]
    if genuine:
        best = min(genuine, key=lambda r: r[1][1])
        print(f"\nDevelopment-MAE winner among EWMA variants: {best[0]}")
        print("Do not select a variant based on holdout metrics.")
    print("\nNo model code, database, or live settings were modified.")
    print("Target windows overlap; metrics are descriptive, not independent-sample significance tests.")


if __name__ == "__main__":
    main()
