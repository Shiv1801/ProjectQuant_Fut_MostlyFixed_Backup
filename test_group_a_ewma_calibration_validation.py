"""Read-only paired EWMA calibration and volatility-spike validation.

Run from project root with venv active:
    python .\\test_group_a_ewma_calibration_validation.py

Reads data/project_trade.db via SQLite mode=ro. Does not import/modify live model code,
database rows, app settings, or order execution. Writes CSV reports under
reports/ewma_calibration_validation/.

Protocol matches the prior Group A evaluation: historical NIFTY50 OHLC, 60 input
closes (59 returns), next 30 one-minute returns as target, 30-minute sampling,
chronological 70/30 split. All calibration fitting and spike thresholds are based
on development data only. Holdout is confirmation only.
"""
from __future__ import annotations
import csv, math, sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
REPORT_DIR = ROOT / "reports" / "ewma_calibration_validation"
INDEX_ID = "NIFTY50"
LOOKBACK_CLOSES = 60
N_RETURNS = LOOKBACK_CLOSES - 1
HORIZON = 30
SAMPLE_EVERY = 30
ANNUALIZATION = math.sqrt(252 * 375)
LAMBDA = 0.94
INIT_N = 14
BOOTSTRAP_REPS = 3000
RNG_SEED = 20261013


def valid_ohlc(row):
    _, o, h, l, c, _volume, _source = row
    vals = [o, h, l, c]
    if not all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in vals):
        return False
    o, h, l, c = map(float, vals)
    return h >= max(o, c) and l <= min(o, c) and h >= l


def vol_pct(returns):
    if len(returns) < 2 or not np.all(np.isfinite(returns)):
        return None
    return float(np.std(returns, ddof=1) * ANNUALIZATION * 100.0)


def ewma_forecast(closes, lam, init_n):
    r = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(r) != N_RETURNS or not np.all(np.isfinite(r)) or init_n < 1 or init_n >= len(r):
        return None
    variance = float(np.mean(r[:init_n] ** 2))
    for x in r[init_n:]:
        variance = lam * variance + (1.0 - lam) * float(x * x)
    value = math.sqrt(max(variance, 0.0)) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) and value >= 0 else None


def metric_row(name, pred, actual, ids):
    pairs = [(float(pred[i]), float(actual[i])) for i in ids if pred[i] is not None and math.isfinite(float(pred[i]))]
    if not pairs:
        return {"variant": name, "n": 0}
    p = np.asarray([z[0] for z in pairs]); y = np.asarray([z[1] for z in pairs]); e = p - y
    corr = float(np.corrcoef(p, y)[0, 1]) if len(p) > 1 and np.std(p) > 0 and np.std(y) > 0 else float("nan")
    return {"variant": name, "n": len(pairs), "mae": float(np.mean(np.abs(e))),
            "rmse": float(np.sqrt(np.mean(e * e))), "bias_pred_minus_actual": float(np.mean(e)),
            "median_abs_error": float(np.median(np.abs(e))), "p90_abs_error": float(np.percentile(np.abs(e), 90)),
            "pearson_corr": corr}


def write_csv(path, rows):
    if not rows: return
    keys = []
    for row in rows:
        for key in row:
            if key not in keys: keys.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}. Run from project root.")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        raw = con.execute("""SELECT minute_bucket, open, high, low, close, volume, source
                            FROM candles WHERE index_id=? AND source='historical'
                            ORDER BY minute_bucket""", (INDEX_ID,)).fetchall()
    finally:
        con.close()
    valid = [r for r in raw if valid_ohlc(r)]
    runs, run = [], []
    prev_dt = prev_date = None
    for row in valid:
        dt = datetime.fromisoformat(row[0])
        if run and ((dt - prev_dt).total_seconds() != 60 or dt.date() != prev_date):
            runs.append(run); run = []
        run.append((row, dt)); prev_dt, prev_date = dt, dt.date()
    if run: runs.append(run)

    cases = []
    for rrun in runs:
        for i in range(LOOKBACK_CLOSES - 1, len(rrun) - HORIZON - 1, SAMPLE_EVERY):
            ins = rrun[i - LOOKBACK_CLOSES + 1:i + 1]
            fut = rrun[i + 1:i + HORIZON + 1]
            if len(ins) != LOOKBACK_CLOSES or len(fut) != HORIZON: continue
            closes = np.asarray([float(x[0][4]) for x in ins], dtype=float)
            future_closes = [float(x[0][4]) for x in fut]
            target = vol_pct(np.diff(np.log(np.asarray([closes[-1]] + future_closes, dtype=float))))
            if target is None or not math.isfinite(target): continue
            cases.append({"timestamp": ins[-1][1].isoformat(sep=" "), "date": ins[-1][1].date().isoformat(),
                          "closes": closes, "target": target, "input_vol": vol_pct(np.diff(np.log(closes)))})
    if len(cases) < 200: raise RuntimeError(f"Only {len(cases)} cases; refusing unreliable evaluation.")
    split = int(len(cases) * 0.70)
    dev = list(range(split)); hold = list(range(split, len(cases)))
    actual = {i: cases[i]["target"] for i in range(len(cases))}
    baseline = {i: ewma_forecast(cases[i]["closes"], LAMBDA, 1) for i in range(len(cases))}
    candidate = {i: ewma_forecast(cases[i]["closes"], LAMBDA, INIT_N) for i in range(len(cases))}
    common = [i for i in range(len(cases)) if baseline[i] is not None and candidate[i] is not None]
    dev = [i for i in dev if i in common]; hold = [i for i in hold if i in common]

    # Fit ordinary least squares calibration only on development data: actual = intercept + slope * raw forecast.
    x = np.asarray([candidate[i] for i in dev], dtype=float)
    y = np.asarray([actual[i] for i in dev], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    calibrated = {i: max(0.0, float(intercept + slope * candidate[i])) for i in common}

    # A secondary proportional calibration (no intercept), also development-only, for comparison.
    denom = float(np.dot(x, x))
    scale = float(np.dot(x, y) / denom) if denom > 0 else 1.0
    scaled = {i: max(0.0, float(scale * candidate[i])) for i in common}

    variants = {"baseline_current_init_1": baseline,
                "candidate_init_14_uncalibrated": candidate,
                "candidate_affine_calibrated_dev_only": calibrated,
                "candidate_scale_calibrated_dev_only": scaled}
    overall = []
    for split_name, ids in [("development_fit_or_in_sample", dev), ("holdout_confirmation", hold)]:
        for name, pred in variants.items():
            overall.append({"split": split_name, **metric_row(name, pred, actual, ids)})

    # Spike thresholds are fixed from development target distribution; evaluate holdout actual future-vol spikes.
    dev_targets = np.asarray([actual[i] for i in dev], dtype=float)
    thresholds = {"p90": float(np.quantile(dev_targets, 0.90)), "p95": float(np.quantile(dev_targets, 0.95)),
                  "p99": float(np.quantile(dev_targets, 0.99))}
    spike_rows = []
    for label, threshold in thresholds.items():
        ids = [i for i in hold if actual[i] >= threshold]
        for name, pred in variants.items():
            row = metric_row(name, pred, actual, ids)
            row.update({"spike_threshold": label, "development_target_threshold": threshold,
                        "holdout_actual_target_min": min((actual[i] for i in ids), default=None),
                        "mean_actual": float(np.mean([actual[i] for i in ids])) if ids else None,
                        "mean_prediction": float(np.mean([pred[i] for i in ids])) if ids else None})
            spike_rows.append(row)

    # Monthly holdout comparison.
    months = defaultdict(list)
    for i in hold: months[cases[i]["date"][:7]].append(i)
    monthly = []
    for month, ids in sorted(months.items()):
        for name, pred in variants.items(): monthly.append({"month": month, **metric_row(name, pred, actual, ids)})

    # Input volatility quintiles defined from development inputs only.
    dev_iv = np.asarray([cases[i]["input_vol"] for i in dev], dtype=float)
    q = np.quantile(dev_iv, [0.2, 0.4, 0.6, 0.8])
    regimes = {i: ["very_low", "low", "medium", "high", "very_high"][int(np.searchsorted(q, cases[i]["input_vol"], side="right"))] for i in hold}
    regime_rows = []
    for regime in ["very_low", "low", "medium", "high", "very_high"]:
        ids = [i for i in hold if regimes[i] == regime]
        for name, pred in variants.items(): regime_rows.append({"input_vol_regime": regime, **metric_row(name, pred, actual, ids)})

    # Paired date-cluster bootstrap for holdout MAE improvements, positive means comparator is beaten.
    by_date = defaultdict(list)
    for i in hold: by_date[cases[i]["date"]].append(i)
    dates = sorted(by_date); rng = np.random.default_rng(RNG_SEED); boot_rows = []
    comparisons = [("candidate_init_14_uncalibrated", candidate),
                   ("candidate_affine_calibrated_dev_only", calibrated),
                   ("candidate_scale_calibrated_dev_only", scaled)]
    for name, pred in comparisons:
        values = []
        for _ in range(BOOTSTRAP_REPS):
            chosen = rng.choice(dates, size=len(dates), replace=True)
            err_diffs = []
            for d in chosen:
                for i in by_date[d]:
                    err_diffs.append(abs(baseline[i] - actual[i]) - abs(pred[i] - actual[i]))
            if err_diffs: values.append(float(np.mean(err_diffs)))
        boot_rows.append({"comparison_vs_baseline": name, "bootstrap_reps": BOOTSTRAP_REPS,
                          "holdout_dates": len(dates), "mean_mae_improvement": float(np.mean(values)),
                          "ci_2_5_pct": float(np.percentile(values, 2.5)), "median": float(np.median(values)),
                          "ci_97_5_pct": float(np.percentile(values, 97.5)),
                          "fraction_bootstraps_positive": float(np.mean(np.asarray(values) > 0))})

    case_rows = []
    for i, c in enumerate(cases):
        case_rows.append({"timestamp": c["timestamp"], "date": c["date"], "split": "development" if i < split else "holdout",
                          "actual_future_vol": c["target"], "input_window_vol": c["input_vol"],
                          "baseline_prediction": baseline[i], "candidate_init14_prediction": candidate[i],
                          "affine_calibrated_prediction": calibrated[i], "scale_calibrated_prediction": scaled[i],
                          "actual_target_p90_or_higher": c["target"] >= thresholds["p90"],
                          "actual_target_p95_or_higher": c["target"] >= thresholds["p95"],
                          "actual_target_p99_or_higher": c["target"] >= thresholds["p99"]})

    diagnostics = [
        {"check":"database_path", "value":str(DB_PATH), "status":"PASS"},
        {"check":"raw_historical_rows", "value":len(raw), "status":"INFO"},
        {"check":"valid_historical_ohlc_rows", "value":len(valid), "status":"PASS" if len(valid)>=1000 else "WARN"},
        {"check":"invalid_historical_ohlc_rows_excluded", "value":len(raw)-len(valid), "status":"INFO"},
        {"check":"contiguous_same_day_runs", "value":len(runs), "status":"INFO"},
        {"check":"total_cases", "value":len(cases), "status":"PASS" if len(cases)>=1000 else "WARN"},
        {"check":"development_cases", "value":len(dev), "status":"INFO"},
        {"check":"holdout_cases", "value":len(hold), "status":"INFO"},
        {"check":"calibration_fit", "value":f"actual = {intercept:.10f} + {slope:.10f} * raw_candidate; fit on development only", "status":"INFO"},
        {"check":"scale_calibration_fit", "value":f"actual = {scale:.10f} * raw_candidate; fit on development only", "status":"INFO"},
        {"check":"p90_target_threshold", "value":thresholds['p90'], "status":"INFO"},
        {"check":"p95_target_threshold", "value":thresholds['p95'], "status":"INFO"},
        {"check":"p99_target_threshold", "value":thresholds['p99'], "status":"INFO"},
        {"check":"database_access", "value":"SQLite mode=ro", "status":"PASS"},
        {"check":"live_model_database_settings_changed", "value":"No", "status":"PASS"},
        {"check":"selection_note", "value":"No holdout-based model selection; calibration parameters fitted on development only", "status":"PASS"},
    ]
    write_csv(REPORT_DIR / "ewma_calibration_overall.csv", overall)
    write_csv(REPORT_DIR / "ewma_calibration_spikes.csv", spike_rows)
    write_csv(REPORT_DIR / "ewma_calibration_monthly.csv", monthly)
    write_csv(REPORT_DIR / "ewma_calibration_regimes.csv", regime_rows)
    write_csv(REPORT_DIR / "ewma_calibration_bootstrap.csv", boot_rows)
    write_csv(REPORT_DIR / "ewma_calibration_predictions.csv", case_rows)
    write_csv(REPORT_DIR / "ewma_calibration_diagnostics.csv", diagnostics)

    print("="*118)
    print("GROUP A — EWMA CALIBRATION + VOLATILITY-SPIKE VALIDATION (READ-ONLY)")
    print("="*118)
    print(f"Database: {DB_PATH}")
    print(f"Raw historical rows: {len(raw):,} | valid OHLC: {len(valid):,} | excluded: {len(raw)-len(valid):,} | runs: {len(runs):,}")
    print(f"Cases: {len(cases):,} | common: {len(common):,} | development: {len(dev):,} | holdout: {len(hold):,}")
    print("Target: next 30 one-minute returns; input 60 closes/59 returns; sample every 30 min; chronological 70/30 split")
    print(f"Candidate: lambda={LAMBDA:.2f}, init={INIT_N}; calibration fits only on development")
    print(f"Affine calibration: actual = {intercept:.8f} + {slope:.8f} * candidate; proportional scale={scale:.8f}")
    print("\nOVERALL METRICS")
    for r in overall:
        print(f"{r['split']:<25} {r['variant']:<38} n={r.get('n',0):4} MAE={r.get('mae',float('nan')):.4f} RMSE={r.get('rmse',float('nan')):.4f} bias={r.get('bias_pred_minus_actual',float('nan')):.4f} corr={r.get('pearson_corr',float('nan')):.4f}")
    print("\nHOLDOUT ACTUAL-VOLATILITY SPIKES (thresholds from development targets)")
    for r in spike_rows:
        if r['variant'] in ("baseline_current_init_1", "candidate_init_14_uncalibrated", "candidate_affine_calibrated_dev_only", "candidate_scale_calibrated_dev_only"):
            print(f"{r['spike_threshold']:<4} {r['variant']:<38} n={r.get('n',0):3} MAE={r.get('mae',float('nan')):.4f} RMSE={r.get('rmse',float('nan')):.4f} bias={r.get('bias_pred_minus_actual',float('nan')):.4f}")
    print("\nDATE-CLUSTER BOOTSTRAP — HOLDOUT PAIRED MAE IMPROVEMENT VS BASELINE")
    for r in boot_rows:
        print(f"{r['comparison_vs_baseline']:<38} mean={r['mean_mae_improvement']:.5f} 95% CI [{r['ci_2_5_pct']:.5f}, {r['ci_97_5_pct']:.5f}] positive={r['fraction_bootstraps_positive']:.1%}")
    print(f"\nReports written to: {REPORT_DIR}")
    print("Read-only DB; no live model, settings, or order execution changed.")
    print("Note: calibration holdout results are confirmation only; overlapping forecast windows remain dependent.")

if __name__ == "__main__":
    main()
