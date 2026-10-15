"""
EWMA lambda sweep for Project Trade Group A.
Run from project root with venv active:
    python .\test_group_a_ewma_sweep.py
Read-only DB access; does not change code, DB, or live settings.
Same case construction as test_group_a_holdout.py.
"""
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
LOOKBACK = 60
HORIZON = 30
SAMPLE_EVERY = 30
ANNUALIZATION = math.sqrt(252 * 375)
LAMBDAS = (0.90, 0.94, 0.97, 0.98, 0.99)


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


def ewma_forecast(closes, lam):
    """Mirror the current EwmaVolatility.compute recursion without changing code."""
    closes = np.asarray(closes, dtype=float)
    if closes.ndim != 1 or closes.size < 3 or not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        return None
    if not 0.0 < lam < 1.0:
        return None
    log_returns = np.diff(np.log(closes))
    if log_returns.size < 2 or not np.all(np.isfinite(log_returns)):
        return None
    variance = float(log_returns[0] ** 2)
    for ret in log_returns[1:]:
        variance = lam * variance + (1.0 - lam) * float(ret * ret)
    value = math.sqrt(variance) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) and value >= 0 else None


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
        for i in range(LOOKBACK - 1, len(run) - HORIZON - 1, SAMPLE_EVERY):
            in_rows = [x[0] for x in run[i - LOOKBACK + 1:i + 1]]
            fut_rows = [x[0] for x in run[i + 1:i + HORIZON + 1]]
            if len(fut_rows) != HORIZON:
                continue
            closes = [float(r[4]) for r in in_rows]
            y = target_vol([closes[-1]] + [float(r[4]) for r in fut_rows])
            if y is not None and math.isfinite(y):
                cases.append((in_rows[-1][0], closes, y))

    if len(cases) < 100:
        raise RuntimeError(f"Only {len(cases)} cases; refusing to report a weak evaluation.")
    split = int(len(cases) * 0.70)
    dev_idx = list(range(split))
    holdout_idx = list(range(split, len(cases)))
    forecasts = {lam: {} for lam in LAMBDAS}
    actual = {i: case[2] for i, case in enumerate(cases)}

    start = time.time()
    for i, (_timestamp, closes, _y) in enumerate(cases):
        for lam in LAMBDAS:
            value = ewma_forecast(closes, lam)
            if value is not None:
                forecasts[lam][i] = value
        if (i + 1) % 1000 == 0:
            print(f"Processed {i+1:,}/{len(cases):,} cases; elapsed {time.time()-start:.1f}s")

    common = set(actual)
    for lam in LAMBDAS:
        common &= set(forecasts[lam])
    dev_common = sorted(i for i in common if i < split)
    holdout_common = sorted(i for i in common if i >= split)

    print("=" * 86)
    print("GROUP A — EWMA LAMBDA SWEEP (READ-ONLY)")
    print("=" * 86)
    print(f"Index: {INDEX_ID} | valid historical OHLC rows: {len(rows):,}")
    print(f"Cases: {len(cases):,} | lookback: {LOOKBACK} min | target: next {HORIZON} min")
    print(f"Development: {cases[0][0]} through {cases[split-1][0]} ({split:,} cases)")
    print(f"Holdout:     {cases[split][0]} through {cases[-1][0]} ({len(cases)-split:,} cases)")
    print(f"Common cases — development: {len(dev_common):,}; holdout: {len(holdout_common):,}")
    print("\nMetrics on identical observations; lower MAE/RMSE is better.")
    print(f"{'Lambda':>8} {'Dev MAE':>10} {'Dev RMSE':>10} {'Holdout MAE':>13} {'Holdout RMSE':>14}")
    print("-" * 62)
    results = []
    for lam in LAMBDAS:
        dev = metric(forecasts[lam], actual, dev_common)
        hold = metric(forecasts[lam], actual, holdout_common)
        if dev and hold:
            results.append((lam, dev, hold))
            print(f"{lam:8.2f} {dev[1]:10.4f} {dev[2]:10.4f} {hold[1]:13.4f} {hold[2]:14.4f}")
    if results:
        chosen = min(results, key=lambda x: x[1][1])
        print(f"\nDevelopment-selected lambda by MAE: {chosen[0]:.2f}")
        print("Use holdout as a one-time final check; do not choose lambda from holdout.")
    print("\nCurrent production code and live settings were not modified.")
    print("Note: future target windows overlap, so metrics are descriptive rather than independent-sample significance tests.")


if __name__ == "__main__":
    main()
