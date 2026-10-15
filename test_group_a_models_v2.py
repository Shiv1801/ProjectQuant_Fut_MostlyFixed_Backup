"""
Group A historical volatility evaluation for Project Trade.

Run from the project root:
    python .\test_group_a_models.py

Read-only: reads data/project_trade.db and does not change project files or DB.
Uses historical NIFTY50 OHLC rows even when historical volume is absent/zero.
It excludes live-source rows from the first pass because the live feed may create
flat placeholder candles after the market closes.

Method:
- Input: prior 60 consecutive one-minute OHLC bars in the same date/session.
- Target: realized volatility of the next 30 one-minute close-to-close returns.
- Sample spacing: one evaluation every 30 minutes to limit GARCH runtime.
- Only price-valid bars and strictly consecutive timestamps are used.
- Model errors are reported as missing forecasts rather than silently discarded.
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


def line(title: str) -> None:
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)


def load_rows():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("""
            SELECT minute_bucket, open, high, low, close, volume, source
            FROM candles
            WHERE index_id = ? AND source = 'historical'
            ORDER BY minute_bucket
        """, (INDEX_ID,)).fetchall()
    finally:
        con.close()


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


def main():
    line("GROUP A — HISTORICAL DATA AND MODEL DIAGNOSTIC")
    print(f"Project root: {ROOT}")
    print(f"Database: {DB_PATH}")
    print(f"Index: {INDEX_ID}")
    print(f"Source filter: historical only (zero volume is not automatically rejected)")
    print(f"Input window: {LOOKBACK} consecutive one-minute bars")
    print(f"Target horizon: next {HORIZON} one-minute returns")
    print(f"Evaluation spacing: every {SAMPLE_EVERY} minutes")

    gk_path = ROOT / "engine" / "signal_layer" / "volatility" / "garman_klass.py"
    gk_syntax_ok = False
    try:
        compile(gk_path.read_text(encoding="utf-8"), str(gk_path), "exec")
        gk_syntax_ok = True
        print("Garman-Klass syntax: PASS")
    except Exception as exc:
        print(f"Garman-Klass syntax: FAIL — {type(exc).__name__}: {exc}")
        print("Garman-Klass will be excluded pending repair.")

    from engine.signal_layer.volatility.realized_vol import compute_realized_vol
    from engine.signal_layer.volatility.ewma import EwmaVolatility
    from engine.signal_layer.volatility.parkinson import ParkinsonEstimator
    from engine.signal_layer.volatility.garch import compute_garch_forecast

    models = {
        "realized_vol": "standalone",
        "ewma": EwmaVolatility(),
        "parkinson": ParkinsonEstimator(),
        "garch": "standalone",
    }
    if gk_syntax_ok:
        try:
            from engine.signal_layer.volatility.garman_klass import GarmanKlassEstimator
            models["garman_klass"] = GarmanKlassEstimator()
        except Exception as exc:
            print(f"Garman-Klass import: FAIL — {type(exc).__name__}: {exc}")

    rows = [r for r in load_rows() if valid_ohlc(r)]
    print(f"Valid historical OHLC rows: {len(rows):,}")
    if not rows:
        print("No valid historical OHLC rows found.")
        return

    # Split at dates and every missing minute; never bridge an overnight gap.
    runs = []
    current = []
    prev_dt = None
    prev_date = None
    for row in rows:
        dt = datetime.fromisoformat(row[0])
        if current and (
            (dt - prev_dt).total_seconds() != 60 or dt.date() != prev_date
        ):
            runs.append(current)
            current = []
        current.append((row, dt))
        prev_dt, prev_date = dt, dt.date()
    if current:
        runs.append(current)

    eligible = []
    for run in runs:
        # Require complete contiguous bars; target starts strictly after input window.
        for i in range(LOOKBACK - 1, len(run) - HORIZON - 1, SAMPLE_EVERY):
            input_rows = [x[0] for x in run[i - LOOKBACK + 1:i + 1]]
            future_rows = [x[0] for x in run[i + 1:i + HORIZON + 1]]
            if len(future_rows) != HORIZON:
                continue
            md = {
                "close_1min": [float(r[4]) for r in input_rows],
                "open_1min": [float(r[1]) for r in input_rows],
                "high_1min": [float(r[2]) for r in input_rows],
                "low_1min": [float(r[3]) for r in input_rows],
            }
            # Include the last observed close as the start price for the 30 future returns.
            y = target_vol([md["close_1min"][-1]] + [float(r[4]) for r in future_rows])
            if y is not None and math.isfinite(y):
                eligible.append((input_rows[-1][0], md, y))

    print(f"Contiguous same-day runs: {len(runs):,}")
    print(f"Eligible forecast cases: {len(eligible):,}")
    if len(eligible) < 30:
        print("Insufficient cases; inspect timestamp gaps and source data before comparing models.")
        return

    line("INDIVIDUAL MODEL SMOKE TEST")
    sample_md = eligible[0][1]
    for name, model in models.items():
        try:
            if name == "realized_vol":
                out = compute_realized_vol(sample_md["close_1min"])
                value, note = out.get("realized_vol_pct"), out.get("note")
            elif name == "garch":
                out = compute_garch_forecast(sample_md["close_1min"])
                value, note = out.get("garch_forecast_vol_pct"), out.get("note")
            else:
                out = model.compute(sample_md)
                value, note = out.score, None
            print(f"{name:16s} value={value!r} note={note!r}")
        except Exception as exc:
            print(f"{name:16s} ERROR {type(exc).__name__}: {exc}")

    line("WALK-FORWARD-STYLE FORECAST EVALUATION")
    print("Target is next-30-minute annualized realized volatility.")
    print("MAE/RMSE are in annualized volatility percentage points.")
    print("This is a diagnostic comparison, not proof of trading profitability.")

    pred = {name: {} for name in models}
    actual = {}
    start = time.time()
    for i, (timestamp, md, y) in enumerate(eligible):
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
                    pred[name][i] = float(val)
            except Exception:
                pass
        if (i + 1) % 100 == 0:
            print(f"Processed {i + 1:,}/{len(eligible):,}; elapsed {time.time()-start:.1f}s")

    def calc(name, idx):
        errors = [pred[name][i] - actual[i] for i in idx]
        return (
            len(errors),
            statistics.mean(abs(e) for e in errors),
            math.sqrt(statistics.mean(e * e for e in errors)),
            statistics.mean(pred[name][i] for i in idx),
            statistics.mean(actual[i] for i in idx),
        ) if errors else None

    # Fair model-to-model comparison: identical timestamps for all models.
    common = set(actual)
    for name in models:
        common &= set(pred[name])
    line("MODEL METRICS — COMMON CASES")
    print(f"Cases where every loaded model produced a forecast: {len(common):,}")
    print(f"{'Model':18s} {'N':>6s} {'MAE':>12s} {'RMSE':>12s} {'Mean pred':>12s} {'Mean actual':>12s}")
    print("-" * 78)
    for name in models:
        m = calc(name, sorted(common))
        if m:
            print(f"{name:18s} {m[0]:6d} {m[1]:12.4f} {m[2]:12.4f} {m[3]:12.4f} {m[4]:12.4f}")
        else:
            print(f"{name:18s} {'0':>6s} {'N/A':>12s}")

    # Equal-weight ensemble on common cases only. Diagnostic baseline, no tuning.
    if common:
        ensemble_errors = [
            statistics.mean(pred[name][i] for name in models) - actual[i]
            for i in sorted(common)
        ]
        mae = statistics.mean(abs(e) for e in ensemble_errors)
        rmse = math.sqrt(statistics.mean(e * e for e in ensemble_errors))
        print(f"\nEqual-weight ensemble: N={len(common):,}, MAE={mae:.4f}, RMSE={rmse:.4f}")
        print("Ensemble weights are not fitted; do not deploy without separate validation.")

    line("FORECAST COVERAGE")
    for name in models:
        print(f"{name:16s}: {len(pred[name]):,}/{len(eligible):,} forecasts")
    print(f"Elapsed: {time.time()-start:.1f}s")
    print("Database was opened read-only; no live model settings were changed.")


if __name__ == "__main__":
    main()
