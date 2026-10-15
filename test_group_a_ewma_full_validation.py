"""Comprehensive read-only EWMA volatility validation for Project Trade Group A.

Run from project root with venv active:
    python .\\test_group_a_ewma_full_validation.py

Reads data/project_trade.db using SQLite read-only mode. Does not import or edit live
model code, alter the database, or change settings. Writes CSV reports only under
reports/ewma_validation/ (creates that report directory if needed).

Protocol:
- historical NIFTY50 OHLC only; validates OHLC and contiguous one-minute sessions
- 60 input closes -> 59 log returns; forecast target = sample std of next 30 1-min returns
- annualization sqrt(252*375), percentage points, matching prior Group A tests
- sample every 30 minutes within contiguous same-day runs
- chronological 70% development / 30% holdout split
- tune on development only; holdout is a final confirmation, never used to select
- grid: lambda 0.90..0.99 by 0.01; EWMA initialization length 1..30 returns
- compares to exact current recursion (first-return initialization, lambda .94),
  first-return initializations at each lambda, and simple realized-vol references
- reports overall metrics, bias, correlation, regime slices, monthly holdout,
  paired date-cluster bootstrap CI, data checks, and machine-readable CSVs
"""
from __future__ import annotations
import csv, math, sqlite3, statistics, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
REPORT_DIR = ROOT / "reports" / "ewma_validation"
INDEX_ID = "NIFTY50"
LOOKBACK_CLOSES = 60
N_RETURNS = LOOKBACK_CLOSES - 1
HORIZON = 30
SAMPLE_EVERY = 30
ANNUALIZATION = math.sqrt(252 * 375)
BASELINE_LAM = 0.94
LAMBDAS = [round(x, 2) for x in np.arange(0.90, 1.00, 0.01)]
INIT_LENGTHS = list(range(1, 31))
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


def ewma_forecast(closes, lam, init_n=1):
    r = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(r) != N_RETURNS or not np.all(np.isfinite(r)) or init_n < 1 or init_n >= len(r):
        return None
    # Mean squared return over first init_n returns initializes variance; rest are recursive EWMA.
    variance = float(np.mean(r[:init_n] ** 2))
    for x in r[init_n:]:
        variance = lam * variance + (1.0 - lam) * float(x * x)
    value = math.sqrt(max(variance, 0.0)) * ANNUALIZATION * 100.0
    return value if math.isfinite(value) and value >= 0 else None


def metric_row(name, pred, actual, indices):
    pairs = [(float(pred[i]), float(actual[i])) for i in indices if i in pred and pred[i] is not None]
    if not pairs:
        return {"variant": name, "n": 0}
    p = np.asarray([x[0] for x in pairs]); y = np.asarray([x[1] for x in pairs])
    e = p - y
    corr = float(np.corrcoef(p, y)[0, 1]) if len(p) > 1 and np.std(p) > 0 and np.std(y) > 0 else float("nan")
    return {"variant": name, "n": len(p), "mae": float(np.mean(np.abs(e))),
            "rmse": float(np.sqrt(np.mean(e * e))), "bias_pred_minus_actual": float(np.mean(e)),
            "median_abs_error": float(np.median(np.abs(e))), "p90_abs_error": float(np.percentile(np.abs(e), 90)),
            "pearson_corr": corr}


def write_csv(path, rows):
    if not rows:
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys: keys.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}. Run this from the project root.")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        raw = con.execute("""SELECT minute_bucket, open, high, low, close, volume, source
                            FROM candles WHERE index_id=? AND source='historical'
                            ORDER BY minute_bucket""", (INDEX_ID,)).fetchall()
    finally:
        con.close()

    valid = [r for r in raw if valid_ohlc(r)]
    invalid_count = len(raw) - len(valid)
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
            # The next 30 close-to-close returns start at the final input close.
            target = vol_pct(np.diff(np.log(np.asarray([closes[-1]] + future_closes, dtype=float))))
            if target is None or not math.isfinite(target): continue
            input_returns = np.diff(np.log(closes))
            input_vol = vol_pct(input_returns)
            cases.append({"timestamp": ins[-1][1].isoformat(sep=" "), "date": ins[-1][1].date().isoformat(),
                          "closes": closes, "target": target, "input_vol": input_vol,
                          "input_abs_move": float(abs(input_returns[-1]) * ANNUALIZATION * 100.0)})
    if len(cases) < 200: raise RuntimeError(f"Only {len(cases)} valid cases; refusing to claim a reliable evaluation.")

    split = int(len(cases) * 0.70)
    dev = list(range(split)); hold = list(range(split, len(cases)))
    actual = {i: cases[i]["target"] for i in range(len(cases))}
    preds = {}
    # Complete lambda x initialization-length grid.
    for lam in LAMBDAS:
        for n in INIT_LENGTHS:
            name = f"ewma_lam_{lam:.2f}_init_{n:02d}"
            preds[name] = {i: ewma_forecast(cases[i]["closes"], lam, n) for i in range(len(cases))}
        name = f"ewma_lam_{lam:.2f}_first_return"
        preds[name] = {i: ewma_forecast(cases[i]["closes"], lam, 1) for i in range(len(cases))}
    # Fixed comparators, including exact baseline .94 first-return.
    preds["baseline_current_lam_0.94_init_1"] = preds["ewma_lam_0.94_first_return"]
    preds["realized_vol_59_returns"] = {i: cases[i]["input_vol"] for i in range(len(cases))}
    preds["mean_square_59_returns"] = {}
    for i, c in enumerate(cases):
        rr = np.diff(np.log(c["closes"]))
        preds["mean_square_59_returns"][i] = float(np.sqrt(np.mean(rr * rr)) * ANNUALIZATION * 100.0)

    # Common-case intersection so every compared variant sees the same rows.
    common = set(range(len(cases)))
    for values in preds.values():
        common &= {i for i, v in values.items() if v is not None and math.isfinite(float(v)) and float(v) >= 0}
    dev_idx = sorted(i for i in common if i < split); hold_idx = sorted(i for i in common if i >= split)
    if len(dev_idx) < 100 or len(hold_idx) < 50: raise RuntimeError("Insufficient common cases after validation.")

    # Development-only selection: MAE first, RMSE second. Holdout never participates.
    grid_names = [f"ewma_lam_{lam:.2f}_init_{n:02d}" for lam in LAMBDAS for n in INIT_LENGTHS]
    dev_rows = [metric_row(n, preds[n], actual, dev_idx) for n in grid_names]
    dev_rows.sort(key=lambda x: (x.get("mae", float("inf")), x.get("rmse", float("inf"))))
    winner = dev_rows[0]["variant"]
    baseline = "baseline_current_lam_0.94_init_1"

    overall_rows = []
    for name in [baseline, winner, "realized_vol_59_returns", "mean_square_59_returns"]:
        overall_rows.append({"split": "development", **metric_row(name, preds[name], actual, dev_idx)})
        overall_rows.append({"split": "holdout_confirmation", **metric_row(name, preds[name], actual, hold_idx)})
    # Top 20 development grid candidates, with holdout shown only after selection diagnostics.
    top_rows = []
    for row in dev_rows[:20]:
        n = row["variant"]
        top_rows.append({**row, "holdout_mae_confirmation": metric_row(n, preds[n], actual, hold_idx).get("mae"),
                         "holdout_rmse_confirmation": metric_row(n, preds[n], actual, hold_idx).get("rmse")})

    # Month-level holdout performance for selected candidate and baseline.
    month_groups = defaultdict(list)
    for i in hold_idx: month_groups[cases[i]["date"][:7]].append(i)
    monthly_rows = []
    for month, ids in sorted(month_groups.items()):
        for name in [baseline, winner]:
            monthly_rows.append({"month": month, **metric_row(name, preds[name], actual, ids)})

    # Regime slices defined from input-window realized volatility quintiles on development only.
    dev_vol = np.asarray([cases[i]["input_vol"] for i in dev_idx if cases[i]["input_vol"] is not None])
    q = np.quantile(dev_vol, [0.2, 0.4, 0.6, 0.8])
    regime_rows = []
    for i in hold_idx:
        v = cases[i]["input_vol"]
        bucket = int(np.searchsorted(q, v, side="right"))
        cases[i]["regime"] = ["very_low", "low", "medium", "high", "very_high"][bucket]
    for regime in ["very_low", "low", "medium", "high", "very_high"]:
        ids = [i for i in hold_idx if cases[i].get("regime") == regime]
        for name in [baseline, winner]:
            regime_rows.append({"input_vol_regime": regime, **metric_row(name, preds[name], actual, ids)})

    # Date-cluster paired bootstrap of absolute-error improvement: positive means winner better.
    by_date = defaultdict(list)
    for i in hold_idx: by_date[cases[i]["date"]].append(i)
    dates = sorted(by_date)
    rng = np.random.default_rng(RNG_SEED)
    improvements = []
    if len(dates) >= 10:
        for _ in range(BOOTSTRAP_REPS):
            chosen = rng.choice(dates, size=len(dates), replace=True)
            diffs = []
            for d in chosen:
                for i in by_date[d]:
                    diffs.append(abs(preds[baseline][i] - actual[i]) - abs(preds[winner][i] - actual[i]))
            if diffs: improvements.append(float(np.mean(diffs)))
    bootstrap = []
    if improvements:
        bootstrap = [{"comparison": f"{winner} vs {baseline}", "bootstrap_reps": BOOTSTRAP_REPS,
                      "holdout_dates": len(dates), "mean_mae_improvement": float(np.mean(improvements)),
                      "ci_2_5_pct": float(np.percentile(improvements, 2.5)),
                      "median": float(np.median(improvements)), "ci_97_5_pct": float(np.percentile(improvements, 97.5)),
                      "fraction_bootstraps_positive": float(np.mean(np.asarray(improvements) > 0)),
                      "interpretation": "positive improvement favors candidate; CI crossing zero means uncertain"}]

    # Case-level predictions for independent audit/reproduction.
    case_rows = []
    for i, c in enumerate(cases):
        case_rows.append({"timestamp": c["timestamp"], "date": c["date"], "split": "development" if i < split else "holdout",
                          "actual_future_vol": c["target"], "input_window_vol": c["input_vol"],
                          "baseline_prediction": preds[baseline][i], "selected_prediction": preds[winner][i],
                          "baseline_abs_error": abs(preds[baseline][i] - c["target"]),
                          "selected_abs_error": abs(preds[winner][i] - c["target"]),
                          "selected_signed_error": preds[winner][i] - c["target"]})

    # Data and protocol diagnostics.
    diagnostics = [
        {"check": "database_path", "value": str(DB_PATH), "status": "PASS"},
        {"check": "raw_historical_rows", "value": len(raw), "status": "INFO"},
        {"check": "valid_historical_ohlc_rows", "value": len(valid), "status": "PASS" if len(valid) >= 1000 else "WARN"},
        {"check": "invalid_historical_ohlc_rows_excluded", "value": invalid_count, "status": "INFO"},
        {"check": "contiguous_same_day_runs", "value": len(runs), "status": "INFO"},
        {"check": "total_cases", "value": len(cases), "status": "PASS" if len(cases) >= 1000 else "WARN"},
        {"check": "development_cases", "value": len(dev_idx), "status": "INFO"},
        {"check": "holdout_cases", "value": len(hold_idx), "status": "INFO"},
        {"check": "common_case_count", "value": len(common), "status": "PASS"},
        {"check": "lambda_grid", "value": ",".join(f"{x:.2f}" for x in LAMBDAS), "status": "INFO"},
        {"check": "initialization_lengths", "value": f"{min(INIT_LENGTHS)}..{max(INIT_LENGTHS)} returns", "status": "INFO"},
        {"check": "selection_rule", "value": "development MAE then RMSE; holdout not used for selection", "status": "PASS"},
        {"check": "db_access", "value": "SQLite mode=ro", "status": "PASS"},
        {"check": "live_model_or_settings_changed", "value": "No", "status": "PASS"},
        {"check": "holdout_date_cluster_bootstrap", "value": f"{BOOTSTRAP_REPS} reps; seed {RNG_SEED}", "status": "INFO"},
    ]

    write_csv(REPORT_DIR / "ewma_grid_development.csv", dev_rows)
    write_csv(REPORT_DIR / "ewma_top20_candidates.csv", top_rows)
    write_csv(REPORT_DIR / "ewma_overall_metrics.csv", overall_rows)
    write_csv(REPORT_DIR / "ewma_monthly_holdout.csv", monthly_rows)
    write_csv(REPORT_DIR / "ewma_regime_holdout.csv", regime_rows)
    write_csv(REPORT_DIR / "ewma_bootstrap_comparison.csv", bootstrap)
    write_csv(REPORT_DIR / "ewma_case_predictions.csv", case_rows)
    write_csv(REPORT_DIR / "ewma_diagnostics.csv", diagnostics)

    base_dev = metric_row(baseline, preds[baseline], actual, dev_idx)
    base_hold = metric_row(baseline, preds[baseline], actual, hold_idx)
    win_dev = metric_row(winner, preds[winner], actual, dev_idx)
    win_hold = metric_row(winner, preds[winner], actual, hold_idx)
    elapsed = time.time()
    print("=" * 112)
    print("GROUP A — COMPREHENSIVE EWMA VALIDATION (READ-ONLY; NO LIVE CHANGES)")
    print("=" * 112)
    print(f"Database: {DB_PATH}")
    print(f"Raw historical rows: {len(raw):,} | valid OHLC: {len(valid):,} | invalid excluded: {invalid_count:,} | runs: {len(runs):,}")
    print(f"Cases: {len(cases):,} | common cases: {len(common):,} | development: {len(dev_idx):,} | holdout: {len(hold_idx):,}")
    print(f"Target: next {HORIZON} 1-minute returns; input {LOOKBACK_CLOSES} closes/{N_RETURNS} returns; sample every {SAMPLE_EVERY} min")
    print(f"Grid: {len(LAMBDAS)} lambdas x {len(INIT_LENGTHS)} init lengths = {len(grid_names)} EWMA candidates")
    print("Selection rule: development MAE, then development RMSE. Holdout used only for confirmation.")
    print("\nTOP 10 CANDIDATES — RANKED ON DEVELOPMENT ONLY")
    print(f"{'Rank':>4} {'Candidate':34s} {'Dev MAE':>9} {'Dev RMSE':>9} {'Hold MAE*':>10} {'Hold RMSE*':>11}")
    print("-" * 84)
    for rank, row in enumerate(top_rows[:10], 1):
        print(f"{rank:4d} {row['variant']:34s} {row['mae']:9.4f} {row['rmse']:9.4f} {row['holdout_mae_confirmation']:10.4f} {row['holdout_rmse_confirmation']:11.4f}")
    print("*Holdout columns are confirmation only and did not affect ranking.")
    print("\nSELECTED CANDIDATE VS CURRENT BASELINE")
    print(f"Candidate: {winner}")
    print(f"{'Split':20s} {'Model':34s} {'N':>6} {'MAE':>9} {'RMSE':>9} {'Bias':>10} {'Corr':>8}")
    for split_name, a, b in [("development", base_dev, win_dev), ("holdout", base_hold, win_hold)]:
        for row in [a, b]:
            print(f"{split_name:20s} {row['variant']:34s} {row['n']:6d} {row['mae']:9.4f} {row['rmse']:9.4f} {row['bias_pred_minus_actual']:10.4f} {row['pearson_corr']:8.4f}")
    if bootstrap:
        b = bootstrap[0]
        print("\nDATE-CLUSTER BOOTSTRAP — HOLDOUT PAIRED MAE IMPROVEMENT")
        print(f"Positive favors candidate. Mean={b['mean_mae_improvement']:.5f}; 95% CI [{b['ci_2_5_pct']:.5f}, {b['ci_97_5_pct']:.5f}]; median={b['median']:.5f}; positive resamples={b['fraction_bootstraps_positive']:.1%}")
        if b['ci_2_5_pct'] <= 0 <= b['ci_97_5_pct']:
            print("Interpretation: interval crosses zero; improvement is uncertain.")
        else:
            print("Interpretation: interval excludes zero in this clustered bootstrap; still require strategy-level/shadow validation.")
    print("\nHOLDOUT MONTHLY AND INPUT-VOLATILITY-REGIME BREAKDOWNS: see CSV reports.")
    print(f"\nReports written to: {REPORT_DIR}")
    print("Read-only DB connection used. No model source, database contents, or live settings were changed.")
    print("Important: this selects a statistical volatility forecast, not necessarily the best trading-strategy outcome.")

if __name__ == "__main__":
    main()
