"""
Read-only Group A diagnostic/backtest harness for Project Trade.

Run from the project root with the project's venv active:
    python test_group_a_models.py

Reads data/project_trade.db only. Does not change live code, DB rows, or settings.
Uses 60 prior one-minute candles and evaluates forecasts against the next 30
one-minute returns, sampling every 60 eligible observations to keep GARCH runtime
manageable. Only windows with consecutive minute timestamps are included.
"""
from __future__ import annotations

import importlib
import math
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
LOOKBACK = 60
HORIZON = 30
SAMPLE_EVERY = 60
ANNUALIZATION = math.sqrt(252 * 375)
INDEX_ID = "NIFTY50"


def line(title: str) -> None:
    print("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)


def load_rows():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("""
            SELECT minute_bucket, open, high, low, close, volume
            FROM candles
            WHERE index_id = ?
            ORDER BY minute_bucket
        """, (INDEX_ID,)).fetchall()
    finally:
        con.close()
    return rows


def parse_minute(value):
    # DB timestamps in this project use YYYY-MM-DD HH:MM:SS.
    from datetime import datetime
    return datetime.fromisoformat(value)


def target_vol(closes):
    # Annualized sample standard deviation of the next HORIZON 1-min log returns.
    returns = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(returns) < 2 or not np.all(np.isfinite(returns)):
        return None
    return float(np.std(returns, ddof=1) * ANNUALIZATION * 100.0)


def valid_ohlc(row):
    # Price-based volatility models do not require volume.
    _, o, h, l, c, _volume = row
    vals = [o, h, l, c]
    try:
        o, h, l, c = [float(v) for v in vals]
    except (TypeError, ValueError):
        return False

    return (
        all(math.isfinite(v) and v > 0 for v in (o, h, l, c))
        and h >= max(o, c)
        and l <= min(o, c)
        and h >= l
    )


def main():
    line("GROUP A — READ-ONLY DATA AND MODEL DIAGNOSTIC")
    print(f"Project root : {ROOT}")
    print(f"Database     : {DB_PATH}")
    print(f"Index        : {INDEX_ID}")
    print(f"Lookback     : {LOOKBACK} consecutive one-minute candles")
    print(f"Target       : next {HORIZON} one-minute returns")
    print(f"Sampling     : every {SAMPLE_EVERY} eligible rows")
    if not DB_PATH.exists():
        raise FileNotFoundError("Expected data/project_trade.db. Run from project root.")

    # Confirm Garman-Klass parses before importing it.
    gk_path = ROOT / "engine" / "signal_layer" / "volatility" / "garman_klass.py"
    gk_syntax_ok = False
    try:
        source = gk_path.read_text(encoding="utf-8")
        compile(source, str(gk_path), "exec")
        gk_syntax_ok = True
        print("Garman-Klass syntax: PASS")
    except Exception as exc:
        print(f"Garman-Klass syntax: FAIL — {type(exc).__name__}: {exc}")
        print("It will be excluded until the file is repaired.")

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

    rows = load_rows()
    print(f"Raw {INDEX_ID} rows: {len(rows):,}")
    if not rows:
        print("No rows available.")
        return

    # Keep valid OHLC rows inside the regular weekday session.
    # This is a time filter, not an exchange-holiday calendar.
    from datetime import time as clock_time

    valid_rows = [r for r in rows if valid_ohlc(r)]
    usable = []
    excluded_session = 0

    for row in valid_rows:
        dt = parse_minute(row[0])
        in_session = (
            dt.weekday() < 5
            and clock_time(9, 15) <= dt.time() <= clock_time(15, 30)
        )
        if in_session:
            usable.append(row)
        else:
            excluded_session += 1

    print(f"Rows with valid OHLC: {len(valid_rows):,}")
    print(f"Rows excluded outside weekday session: {excluded_session:,}")
    print(f"Rows retained inside weekday session: {len(usable):,}")

    if not usable:
        print("No in-session OHLC candles. Cannot evaluate.")
        return

    # Split into contiguous runs so no rolling window crosses gaps or sessions.
    runs = []
    current = []
    previous_dt = None
    previous_date = None
    for row in usable:
        dt = parse_minute(row[0])
        if (
            current
            and (
                previous_dt is None
                or (dt - previous_dt).total_seconds() != 60
                or dt.date() != previous_date
            )
        ):
            runs.append(current)
            current = []
        current.append((row, dt))
        previous_dt = dt
        previous_date = dt.date()
    if current:
        runs.append(current)

    eligible = []
    for run in runs:
        # i marks the end of input window; target is strictly after i.
        for i in range(LOOKBACK - 1, len(run) - HORIZON - 1, SAMPLE_EVERY):
            input_rows = [item[0] for item in run[i - LOOKBACK + 1:i + 1]]
            future_rows = [item[0] for item in run[i + 1:i + HORIZON + 1]]
            if len(future_rows) != HORIZON:
                continue
            input_closes = [float(r[4]) for r in input_rows]
            future_closes = [float(input_rows[-1][4])] + [float(r[4]) for r in future_rows]
            y = target_vol(future_closes)
            if y is None or not math.isfinite(y):
                continue
            md = {
                "close_1min": input_closes,
                "open_1min": [float(r[1]) for r in input_rows],
                "high_1min": [float(r[2]) for r in input_rows],
                "low_1min": [float(r[3]) for r in input_rows],
            }
            eligible.append((input_rows[-1][0], md, y))

    print(f"Contiguous sessions/runs: {len(runs):,}")
    print(f"Out-of-sample forecast cases: {len(eligible):,}")
    if len(eligible) < 30:
        print("Not enough eligible samples for a useful performance comparison.")
        return

    # Individual functional smoke test with a representative window.
    line("INDIVIDUAL MODEL SMOKE TEST")
    sample_md = eligible[0][1]
    for name, model in models.items():
        try:
            if name == "realized_vol":
                out = compute_realized_vol(sample_md["close_1min"])
                score = out.get("realized_vol_pct")
                note = out.get("note")
            elif name == "garch":
                out = compute_garch_forecast(sample_md["close_1min"])
                score = out.get("garch_forecast_vol_pct")
                note = out.get("note")
            else:
                out = model.compute(sample_md)
                score = out.score
                note = out.feature_vector.get("note") if isinstance(out.feature_vector, dict) else None
            print(f"{name:16s} score={score!r} note={note!r}")
        except Exception as exc:
            print(f"{name:16s} ERROR {type(exc).__name__}: {exc}")

    # Forecast comparison. Models are evaluated at regular spaced timestamps;
    # every forecast sees only the prior 60 candles. GARCH can be slow.
    line("HISTORICAL FORECAST EVALUATION")
    print("Metric: MAE / RMSE against next-30-minute annualized realized volatility.")
    print("Naive baseline: trailing 60-return realized volatility.")
    print("Note: this is a first walk-forward-style evaluation, not a tuned strategy backtest.")

    predictions = {name: [] for name in models}
    actuals = []
    timestamps = []
    started = time.time()
    for j, (timestamp, md, actual) in enumerate(eligible, 1):
        actuals.append(actual)
        timestamps.append(timestamp)
        for name, model in models.items():
            try:
                if name == "realized_vol":
                    value = compute_realized_vol(md["close_1min"]).get("realized_vol_pct")
                elif name == "garch":
                    value = compute_garch_forecast(md["close_1min"]).get("garch_forecast_vol_pct")
                else:
                    value = model.compute(md).score
                if value is not None and math.isfinite(float(value)) and float(value) >= 0:
                    predictions[name].append((j - 1, float(value)))
            except Exception:
                # Keep evaluation running; missing forecasts are reported as coverage loss.
                continue
        if j % 100 == 0:
            print(f"Processed {j:,}/{len(eligible):,} cases... elapsed {time.time()-started:.1f}s")

    # Common sample set avoids comparing models on different market timestamps.
    pred_maps = {name: dict(items) for name, items in predictions.items()}
    common_indices = set(range(len(actuals)))
    for name in models:
        common_indices &= set(pred_maps[name])
    print(f"\nCommon cases across all available models: {len(common_indices):,}")

    def metrics(indices, values):
        errors = [values[i] - actuals[i] for i in indices]
        if not errors:
            return None
        return (
            sum(abs(e) for e in errors) / len(errors),
            math.sqrt(sum(e * e for e in errors) / len(errors)),
            statistics.mean(values[i] for i in indices),
            statistics.mean(actuals[i] for i in indices),
        )

    def report(indices):
        print(f"{'Model':18s} {'N':>6s} {'MAE':>12s} {'RMSE':>12s} {'Mean forecast':>15s} {'Mean target':>13s}")
        print("-" * 82)
        for name in models:
            idx = [i for i in indices if i in pred_maps[name]]
            values = pred_maps[name]
            m = metrics(idx, values)
            if m is None:
                print(f"{name:18s} {len(idx):6d} {'N/A':>12s}")
            else:
                print(f"{name:18s} {len(idx):6d} {m[0]:12.4f} {m[1]:12.4f} {m[2]:15.4f} {m[3]:13.4f}")

    report(common_indices)

    # Simple equal-weight ensemble of all available forecasts on same cases.
    # This is diagnostic only; ensemble weighting is not fitted to the test data.
    if len(common_indices) > 0:
        ensemble = {}
        for i in common_indices:
            ensemble[i] = statistics.mean(pred_maps[name][i] for name in models)
        line("EQUAL-WEIGHT GROUP A ENSEMBLE (DIAGNOSTIC ONLY)")
        m = metrics(sorted(common_indices), ensemble)
        print(f"Common N={len(common_indices)} | MAE={m[0]:.4f} | RMSE={m[1]:.4f}")
        print("Do not deploy this ensemble until a separate training/validation/test split is used.")

    line("COMPLETED")
    print(f"Elapsed: {time.time()-started:.1f}s")
    print("No database rows or live settings were modified.")


if __name__ == "__main__":
    main()
