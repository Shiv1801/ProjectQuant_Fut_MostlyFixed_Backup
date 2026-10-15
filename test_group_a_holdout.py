"""
Chronological holdout evaluation for Group A volatility estimators.
Run from the Project Trade root with venv active:
    python .\test_group_a_holdout.py

Read-only DB access. No project/live settings are changed.
Uses historical-source NIFTY50 OHLC candles, 60-bar lookback, next-30-return target,
30-minute sampling, and chronological 70% development / 30% holdout split.
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
LOOKBACK = 60
HORIZON = 30
SAMPLE_EVERY = 30
ANNUALIZATION = math.sqrt(252 * 375)


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


def metrics(values, actual, indices):
    errors = [values[i] - actual[i] for i in indices if i in values]
    if not errors:
        return None
    return (
        len(errors),
        statistics.mean(abs(e) for e in errors),
        math.sqrt(statistics.mean(e * e for e in errors)),
        statistics.mean(values[i] for i in indices if i in values),
        statistics.mean(actual[i] for i in indices if i in values),
    )


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}. Run from project root.")

    # Ensure malformed Garman-Klass never silently enters the evaluation.
    gk_path = ROOT / "engine" / "signal_layer" / "volatility" / "garman_klass.py"
    compile(gk_path.read_text(encoding="utf-8"), str(gk_path), "exec")

    from engine.signal_layer.volatility.realized_vol import compute_realized_vol
    from engine.signal_layer.volatility.ewma import EwmaVolatility
    from engine.signal_layer.volatility.parkinson import ParkinsonEstimator
    from engine.signal_layer.volatility.garch import compute_garch_forecast
    from engine.signal_layer.volatility.garman_klass import GarmanKlassEstimator

    models = {
        "realized_vol": "standalone",
        "ewma": EwmaVolatility(),
        "parkinson": ParkinsonEstimator(),
        "garch": "standalone",
        "garman_klass": GarmanKlassEstimator(),
    }

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
            md = {
                "close_1min": [float(r[4]) for r in in_rows],
                "open_1min": [float(r[1]) for r in in_rows],
                "high_1min": [float(r[2]) for r in in_rows],
                "low_1min": [float(r[3]) for r in in_rows],
            }
            y = target_vol([md["close_1min"][-1]] + [float(r[4]) for r in fut_rows])
            if y is not None and math.isfinite(y):
                cases.append((in_rows[-1][0], md, y))

    if len(cases) < 100:
        raise RuntimeError(f"Only {len(cases)} cases; refusing to report a weak holdout.")

    print("=" * 78)
    print("GROUP A — CHRONOLOGICAL HOLDOUT EVALUATION")
    print("=" * 78)
    print(f"Index: {INDEX_ID} | Valid historical OHLC rows: {len(rows):,}")
    print(f"Forecast cases: {len(cases):,} | Lookback: {LOOKBACK} min | Target: {HORIZON} min")
    split = int(len(cases) * 0.70)
    print(f"Development period: {cases[0][0]} through {cases[split-1][0]} ({split:,} cases)")
    print(f"Holdout period:     {cases[split][0]} through {cases[-1][0]} ({len(cases)-split:,} cases)")
    print("No model weights are fitted. Holdout is the latest 30% of chronological cases.")

    forecasts = {name: {} for name in models}
    actual = {}
    start = time.time()
    for i, (timestamp, md, y) in enumerate(cases):
        actual[i] = y
        for name, model in models.items():
            try:
                if name == "realized_vol":
                    val = compute_realized_vol(md["close_1min"]).get("realized_vol_pct")
                elif name == "garch":
                    val = compute_garch_forecast(md["close_1min"]).get("garch_forecast_vol_pct")
                else:
                    val = model.compute(md).score
                if val is not None and math.isfinite(float(val)) and float(val) >= 0:
                    forecasts[name][i] = float(val)
            except Exception:
                pass
        if (i + 1) % 500 == 0:
            print(f"Processed {i+1:,}/{len(cases):,}; elapsed {time.time()-start:.1f}s")

    common = set(actual)
    for name in models:
        common &= set(forecasts[name])
    dev_common = sorted(i for i in common if i < split)
    test_common = sorted(i for i in common if i >= split)
    print(f"\nAll-five-model common cases: {len(common):,}")
    print(f"Development common cases: {len(dev_common):,}")
    print(f"Holdout common cases: {len(test_common):,}")

    ensemble = {
        i: statistics.mean(forecasts[name][i] for name in models)
        for i in common
    }
    report_models = list(models.keys()) + ["ensemble"]
    print("\nMetrics use identical timestamps across all five models and ensemble.")
    print(f"{'Period':12s} {'Model':18s} {'N':>6s} {'MAE':>10s} {'RMSE':>10s} {'Mean pred':>11s} {'Mean target':>12s}")
    print("-" * 91)
    for period, idx in [("DEVELOPMENT", dev_common), ("HOLDOUT", test_common), ("ALL COMMON", sorted(common))]:
        for name in report_models:
            values = ensemble if name == "ensemble" else forecasts[name]
            m = metrics(values, actual, idx)
            if m:
                print(f"{period:12s} {name:18s} {m[0]:6d} {m[1]:10.4f} {m[2]:10.4f} {m[3]:11.4f} {m[4]:12.4f}")
            else:
                print(f"{period:12s} {name:18s} {'0':>6s} {'N/A':>10s}")

    print("\nForecast coverage:")
    for name in models:
        print(f"{name:16s}: {len(forecasts[name]):,}/{len(cases):,}")
    print(f"Ensemble common cases only: {len(ensemble):,}")
    print(f"Elapsed: {time.time()-start:.1f}s")
    print("Read-only evaluation completed; no DB or live settings were modified.")
    print("Interpret holdout results as preliminary: target volatility is noisy and regime-dependent.")


if __name__ == "__main__":
    main()
