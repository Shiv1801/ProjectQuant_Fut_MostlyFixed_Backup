"""
Annual Multi-Index Sweep — runs the reduced grid (11 SL x 12 TimeStop x
10 Risk x 5 Confidence = 6,600 combos, TSL Activation/Base fixed at
5%/3%) across NIFTY50, BANKNIFTY, FINNIFTY, over ~1 year of data, in
BOTH full-history and walk-forward modes, with:
  - Monthwise breakdown per combination (not just one aggregate number)
  - Full-year cumulative totals
  - A consistency score (how many months were profitable, how much
    monthly returns vary) — a stronger robustness check than a single
    total return number, directly targeting the overfitting problem
    seen throughout prior sweeps
  - Stage 2: takes each index's single best (by walk-forward Test
    composite score) combination and simulates all three running
    SIMULTANEOUSLY as one combined ₹150k portfolio (₹50k/index),
    on the same calendar timeline, so combined drawdown reflects
    real overlapping bad months, not just summed individual numbers
  - A separate (B) simple summary table just totaling/averaging each
    index's individual best result, for quick comparison alongside (A)

Capital: Rs 50,000 PER INDEX independently (not shared/pooled) for
Stage 1. Stage 2 combines three independent Rs 50,000 pools into one
Rs 150,000 combined portfolio view.

All output files use the "annual_sweep_v1_<timestamp>_" prefix so they
never collide with earlier standalone_backtest.py runs or each other
across re-runs.

Usage:
    python scripts/annual_sweep.py
"""
import sys
import csv
import json
import itertools
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from config.settings import settings
from engine.data_layer.fyers_client.auth import get_fyers_model
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

TOKEN_PATH = Path(".fyers_token")
CACHE_DIR = Path("annual_sweep_cache")
CACHE_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_PREFIX = f"annual_sweep_v1_{RUN_ID}_"

SYMBOLS = {"NIFTY50": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX"}
INDICES = list(SYMBOLS.keys())

# ---------------- Reduced grid, as specified ----------------
HARD_SL_VALUES = [round(x, 1) for x in np.arange(5.0, 10.5, 0.5)]   # 5-10, step 0.5 -> 11 values
TIME_STOP_VALUES = list(range(5, 65, 5))                             # 5-60, step 5 -> 12 values
TSL_ACTIVATION = 5.0                                                 # fixed
TSL_BASE_TRAIL = 3.0                                                 # fixed
RISK_PER_TRADE_VALUES = list(range(1, 11))                           # 1-10, step 1 -> 10 values
MIN_CONFIDENCE_VALUES = [round(x, 2) for x in np.arange(0.8, 1.05, 0.05)]  # 0.8-1.0, step 0.05 -> 5 values

MIN_TRADES_FILTER = 5
CAPITAL_PER_INDEX = 50000.0
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
DAYS_BACK = 365


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def read_token() -> str:
    if not TOKEN_PATH.exists():
        log("ERROR: No .fyers_token file found. Log into the main app first.")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def fetch_one_year_candles(index_id: str) -> list[dict]:
    """
    Fyers limits 1-min history to ~100 days per request, so a full year
    requires chaining ~4 sequential requests covering different date
    ranges, then concatenating in chronological order.
    """
    token = read_token()
    fyers = get_fyers_model(token)
    fyers_symbol = SYMBOLS[index_id]

    end_date = datetime.now()
    all_candles = []
    chunk_days = 90  # safely under the ~100 day limit
    remaining_days = DAYS_BACK
    chunk_end = end_date

    log(f"Fetching {DAYS_BACK} days of 1-min candles for {index_id} in ~{chunk_days}-day chunks...")
    while remaining_days > 0:
        this_chunk = min(chunk_days, remaining_days)
        chunk_start = chunk_end - timedelta(days=this_chunk)

        data = {
            "symbol": fyers_symbol, "resolution": "1", "date_format": "1",
            "range_from": chunk_start.strftime("%Y-%m-%d"), "range_to": chunk_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        response = fyers.history(data=data)
        if response.get("s") != "ok":
            log(f"  WARNING: chunk {chunk_start.date()} to {chunk_end.date()} failed: {response}")
        else:
            chunk_candles = [
                {"epoch": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
                for c in response.get("candles", [])
            ]
            all_candles = chunk_candles + all_candles  # prepend, since we're going backward in time
            log(f"  Fetched {len(chunk_candles)} candles for {chunk_start.date()} to {chunk_end.date()}")

        chunk_end = chunk_start
        remaining_days -= this_chunk
        time.sleep(0.5)  # be polite to Fyers between chunked requests

    log(f"Total candles for {index_id}: {len(all_candles)}")
    return all_candles


def get_candles(index_id: str, force_refresh: bool) -> list[dict]:
    cache_file = CACHE_DIR / f"{index_id}_1yr_candles.json"
    if cache_file.exists() and not force_refresh:
        log(f"Using cached candles from {cache_file}")
        return json.loads(cache_file.read_text())
    candles = fetch_one_year_candles(index_id)
    cache_file.write_text(json.dumps(candles))
    return candles


def _month_key_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m")


# ---------------- Signal precomputation (same vectorization insight as before) ----------------

def precompute_signals(closes: np.ndarray):
    n = len(closes)
    directions = [None] * n
    confidences = np.zeros(n)
    for i in range(HURST_WINDOW, n):
        window = closes[max(0, i - HURST_WINDOW):i]
        hurst_result = compute_hurst_exponent(window.tolist())
        zscore_result = compute_zscore(window[-ZSCORE_WINDOW:].tolist(), window=ZSCORE_WINDOW)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)
        directions[i] = quant_result.get("direction")
        confidences[i] = quant_result.get("confidence", 0.0)
    return directions, confidences


def precompute_chart_confirmations(candles: list[dict], directions: list):
    n = len(candles)
    confirmed = [False] * n
    for i in range(HURST_WINDOW, n):
        if directions[i] is None:
            continue
        chart_slice = candles[max(0, i - HURST_WINDOW):i + 1]
        result = confirm_chart_structure(chart_slice, directions[i])
        confirmed[i] = result.get("confirmed", False)
    return confirmed


# ---------------- Core per-combination simulation, now with monthwise tracking ----------------

def run_single_combination(candles: list[dict], directions: list, confidences: np.ndarray,
                            confirmed: list, index_id: str, hard_sl_pct: float, time_stop_minutes: int,
                            risk_per_trade_pct: float, min_confidence: float,
                            initial_capital: float = CAPITAL_PER_INDEX,
                            approx_entry_premium: float = 60.0, approx_delta: float = 0.35) -> dict:
    lot_size = LOT_SIZES.get(index_id)
    closes = np.array([c["close"] for c in candles])
    n = len(candles)

    trades = []  # each trade tagged with its month
    open_trade = None
    capital = initial_capital
    tsl_k = 0.15
    tsl_floor_pct = 2.0

    for i in range(n):
        current_price = closes[i]

        if open_trade is not None:
            index_move = current_price - open_trade["entry_index_price"]
            if open_trade["direction"] == "long_put":
                index_move = -index_move
            sim_premium = max(0.01, open_trade["entry_premium"] + index_move * approx_delta)
            profit_pct = (sim_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            elapsed = i - open_trade["entry_index"]
            drop_pct = (open_trade["entry_premium"] - sim_premium) / open_trade["entry_premium"] * 100

            hard_hit = drop_pct >= hard_sl_pct
            time_hit = elapsed >= time_stop_minutes and open_trade["peak_profit_pct"] <= 0

            tsl_active = open_trade["peak_profit_pct"] >= TSL_ACTIVATION
            if tsl_active:
                profit_above_activation = open_trade["peak_profit_pct"] - TSL_ACTIVATION
                trail_distance = max(tsl_floor_pct, TSL_BASE_TRAIL - tsl_k * profit_above_activation)
                locked_floor = open_trade["peak_profit_pct"] - trail_distance
                tsl_hit = profit_pct <= locked_floor
            else:
                tsl_hit = False

            if hard_hit or time_hit or tsl_hit:
                reason = "hard_sl_hit" if hard_hit else ("time_stop" if time_hit else "tsl_hit")
                pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                capital += pnl
                month = _month_key_from_epoch(candles[i].get("epoch", 0)) if candles[i].get("epoch") else "unknown"
                trades.append({"pnl": pnl, "pnl_pct": profit_pct, "reason": reason, "month": month})
                open_trade = None
            continue

        direction = directions[i]
        if direction is None or confidences[i] < min_confidence or not confirmed[i]:
            continue

        sizing = compute_position_size(
            available_capital=capital, index_id=index_id, premium_per_share=approx_entry_premium,
            hard_sl_pct=hard_sl_pct, risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue

        open_trade = {
            "entry_index": i, "entry_index_price": current_price, "entry_premium": approx_entry_premium,
            "direction": direction, "lots": sizing["lots"], "peak_profit_pct": 0.0,
        }

    return _summarize_with_monthwise(trades, initial_capital, open_trade is not None)


def _summarize_with_monthwise(trades: list[dict], initial_capital: float, still_open_at_end: bool) -> dict:
    if not trades:
        note = "one trade opened but never closed within this data window" if still_open_at_end else None
        return {"trade_count": 0, "win_rate": None, "return_pct": None, "max_drawdown_pct": None,
                "avg_win_pct": None, "avg_loss_pct": None, "final_capital": initial_capital,
                "monthwise": {}, "consistency_score": None, "profitable_months": None, "total_months": 0, "note": note}

    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)

    running = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for t in trades:
        running += t["pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Monthwise breakdown
    by_month = defaultdict(list)
    for t in trades:
        by_month[t["month"]].append(t)

    monthwise = {}
    monthly_returns = []
    for month, month_trades in sorted(by_month.items()):
        month_pnl = sum(t["pnl"] for t in month_trades)
        month_wins = [t for t in month_trades if t["pnl"] > 0]
        month_return_pct = round(month_pnl / initial_capital * 100, 3)
        monthwise[month] = {
            "trades": len(month_trades),
            "win_rate": round(len(month_wins) / len(month_trades), 4),
            "pnl": round(month_pnl, 2),
            "return_pct": month_return_pct,
        }
        monthly_returns.append(month_return_pct)

    # Consistency score: fraction of active months that were profitable,
    # penalized by how much monthly returns vary (high variance = less
    # trustworthy even if the total looks good) — directly targets the
    # overfitting pattern seen throughout prior sweeps (great total,
    # but driven by one or two extreme months).
    profitable_months = sum(1 for r in monthly_returns if r > 0)
    total_months = len(monthly_returns)
    profitable_month_ratio = profitable_months / total_months if total_months > 0 else 0

    if len(monthly_returns) > 1:
        mean_return = sum(monthly_returns) / len(monthly_returns)
        variance = sum((r - mean_return) ** 2 for r in monthly_returns) / len(monthly_returns)
        std_dev = variance ** 0.5
        # Normalize: lower relative std dev -> higher consistency
        consistency_score = profitable_month_ratio * max(0.0, 1.0 - min(std_dev / (abs(mean_return) + 5), 1.0))
    else:
        consistency_score = profitable_month_ratio

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "return_pct": round(total_pnl / initial_capital * 100, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(abs(t["pnl_pct"]) for t in trades if t["pnl"] <= 0) / max(1, len(trades) - len(wins)), 3) if len(trades) > len(wins) else None,
        "final_capital": round(initial_capital + total_pnl, 2),
        "monthwise": monthwise,
        "consistency_score": round(consistency_score, 4),
        "profitable_months": profitable_months,
        "total_months": total_months,
        "note": None,
    }


# ---------------- Stage 1: full sweep per index ----------------

def run_stage1_for_index(index_id: str, candles: list[dict]):
    closes = np.array([c["close"] for c in candles])
    combos = list(itertools.product(HARD_SL_VALUES, TIME_STOP_VALUES, RISK_PER_TRADE_VALUES, MIN_CONFIDENCE_VALUES))
    log(f"{index_id}: {len(combos):,} combinations to test")

    segments = {}
    split = int(len(candles) * 0.7)
    segments["full"] = (candles, closes)
    segments["walkforward_test"] = (candles[split:], closes[split:])
    segments["walkforward_train"] = (candles[:split], closes[:split])

    results_by_mode = {}
    for mode_name, (seg_candles, seg_closes) in segments.items():
        log(f"  Precomputing signals for {mode_name} ({len(seg_candles)} candles)...")
        t0 = time.time()
        directions, confidences = precompute_signals(seg_closes)
        confirmed = precompute_chart_confirmations(seg_candles, directions)
        log(f"  Signal precompute done in {time.time()-t0:.1f}s")

        log(f"  Running {len(combos):,} combos for {mode_name}...")
        t0 = time.time()
        mode_results = []
        for idx, (sl, ts, risk, conf) in enumerate(combos):
            r = run_single_combination(seg_candles, directions, confidences, confirmed, index_id,
                                         hard_sl_pct=sl, time_stop_minutes=ts, risk_per_trade_pct=risk, min_confidence=conf)
            r.update({"hard_sl_pct": sl, "time_stop_minutes": ts, "risk_per_trade_pct": risk, "min_confidence": conf})
            mode_results.append(r)
            if (idx + 1) % 1000 == 0:
                elapsed = time.time() - t0
                rate = (idx + 1) / elapsed
                eta = (len(combos) - idx - 1) / rate / 60
                log(f"    [{idx+1:,}/{len(combos):,}] {rate:.0f}/sec, ETA {eta:.1f} min")

        results_by_mode[mode_name] = mode_results
        log(f"  {mode_name} done in {(time.time()-t0)/60:.1f} min")

        _write_full_csv(mode_results, f"{OUTPUT_PREFIX}{index_id}_{mode_name}_full.csv")
        _write_monthwise_csv(mode_results, f"{OUTPUT_PREFIX}{index_id}_{mode_name}_monthwise.csv")

    return results_by_mode


def _write_full_csv(results: list[dict], path: str):
    rows = []
    for r in results:
        row = {k: v for k, v in r.items() if k != "monthwise"}
        rows.append(row)
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_monthwise_csv(results: list[dict], path: str):
    rows = []
    for r in results:
        base = {"hard_sl_pct": r["hard_sl_pct"], "time_stop_minutes": r["time_stop_minutes"],
                 "risk_per_trade_pct": r["risk_per_trade_pct"], "min_confidence": r["min_confidence"]}
        for month, m in r.get("monthwise", {}).items():
            rows.append({**base, "month": month, **m})
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def find_best_combination(results: list[dict]) -> dict | None:
    """Ranks by a composite of walk-forward Test return, consistency
    score, and drawdown — the primary decision metric, not full-history."""
    qualified = [r for r in results if r["trade_count"] and r["trade_count"] >= MIN_TRADES_FILTER]
    if not qualified:
        return None

    def score(r):
        return_component = max(-1.0, min(1.0, r["return_pct"] / 30.0))
        consistency_component = r.get("consistency_score") or 0.0
        dd_component = max(0.0, 1.0 - r["max_drawdown_pct"] / 25.0)
        return 0.35 * return_component + 0.35 * consistency_component + 0.30 * dd_component

    qualified.sort(key=score, reverse=True)
    return qualified[0]


# ---------------- Stage 2: combined portfolio simulation ----------------

def run_stage2_combined(best_per_index: dict, candles_by_index: dict):
    log("\n--- STAGE 2: Combined portfolio simulation (A) ---")
    combined_trades = []
    per_index_summary = {}

    for index_id, best in best_per_index.items():
        if best is None:
            log(f"  {index_id}: no qualifying combination, skipped from combined portfolio")
            continue
        candles = candles_by_index[index_id]
        closes = np.array([c["close"] for c in candles])
        directions, confidences = precompute_signals(closes)
        confirmed = precompute_chart_confirmations(candles, directions)

        result = run_single_combination(
            candles, directions, confidences, confirmed, index_id,
            hard_sl_pct=best["hard_sl_pct"], time_stop_minutes=best["time_stop_minutes"],
            risk_per_trade_pct=best["risk_per_trade_pct"], min_confidence=best["min_confidence"],
            initial_capital=CAPITAL_PER_INDEX,
        )
        per_index_summary[index_id] = result
        log(f"  {index_id} (its own best combo): return={result['return_pct']}%, trades={result['trade_count']}")

    total_initial = CAPITAL_PER_INDEX * len(per_index_summary)
    total_final = sum(r["final_capital"] for r in per_index_summary.values())
    combined_return_pct = round((total_final - total_initial) / total_initial * 100, 3) if total_initial > 0 else None

    # (B) simple summary: sum/average
    summary_b = {
        "total_initial_capital": total_initial,
        "total_final_capital": round(total_final, 2),
        "combined_return_pct": combined_return_pct,
        "avg_win_rate": round(sum(r["win_rate"] for r in per_index_summary.values() if r["win_rate"] is not None) /
                              max(1, sum(1 for r in per_index_summary.values() if r["win_rate"] is not None)), 4)
                        if any(r["win_rate"] is not None for r in per_index_summary.values()) else None,
        "per_index": {idx: {"return_pct": r["return_pct"], "trades": r["trade_count"], "win_rate": r["win_rate"]} for idx, r in per_index_summary.items()},
    }

    return summary_b, per_index_summary


def main():
    log("=" * 70)
    log(f"ANNUAL MULTI-INDEX SWEEP — run id: {RUN_ID}")
    log(f"Grid: {len(HARD_SL_VALUES)} SL x {len(TIME_STOP_VALUES)} TimeStop x "
        f"{len(RISK_PER_TRADE_VALUES)} Risk x {len(MIN_CONFIDENCE_VALUES)} Confidence = "
        f"{len(HARD_SL_VALUES)*len(TIME_STOP_VALUES)*len(RISK_PER_TRADE_VALUES)*len(MIN_CONFIDENCE_VALUES):,} combos/index/mode")
    log(f"Capital: Rs {CAPITAL_PER_INDEX:,.0f} per index. Data: ~{DAYS_BACK} days.")
    log("=" * 70)

    refresh = input("\nForce fresh Fyers data fetch for all indices? (y/n): ").strip().lower() == "y"
    confirm = input("Proceed with full run (this covers all 3 indices, both modes)? (y/n): ").strip().lower()
    if confirm != "y":
        log("Cancelled.")
        return

    t_start = time.time()
    candles_by_index = {}
    best_per_index = {}
    all_results = {}

    for index_id in INDICES:
        log(f"\n{'='*70}\n{index_id}\n{'='*70}")
        candles = get_candles(index_id, force_refresh=refresh)
        if len(candles) < 300:
            log(f"  SKIPPING {index_id}: only {len(candles)} candles, insufficient.")
            continue
        candles_by_index[index_id] = candles

        results_by_mode = run_stage1_for_index(index_id, candles)
        all_results[index_id] = results_by_mode

        best = find_best_combination(results_by_mode["walkforward_test"])
        best_per_index[index_id] = best
        if best:
            log(f"\n  {index_id} BEST (by walk-forward Test): SL={best['hard_sl_pct']} "
                f"TS={best['time_stop_minutes']} Risk={best['risk_per_trade_pct']} Conf={best['min_confidence']} "
                f"-> return={best['return_pct']}%, consistency={best['consistency_score']}, "
                f"profitable_months={best['profitable_months']}/{best['total_months']}")
        else:
            log(f"\n  {index_id}: no combination met the minimum trade filter")

    summary_b, per_index_stage2 = run_stage2_combined(best_per_index, candles_by_index)

    final_report = {
        "run_id": RUN_ID, "generated_at": datetime.now().isoformat(),
        "grid_size_per_index_per_mode": len(HARD_SL_VALUES) * len(TIME_STOP_VALUES) * len(RISK_PER_TRADE_VALUES) * len(MIN_CONFIDENCE_VALUES),
        "capital_per_index": CAPITAL_PER_INDEX,
        "best_per_index": best_per_index,
        "combined_summary_B": summary_b,
        "combined_simulation_A_per_index_detail": per_index_stage2,
    }
    report_path = f"{OUTPUT_PREFIX}SUMMARY.json"
    with open(report_path, "w") as f:
        json.dump(final_report, f, indent=2, default=str)

    total_time = (time.time() - t_start) / 60
    log(f"\n\nTOTAL RUNTIME: {total_time:.1f} minutes")
    log(f"\nAll output files prefixed with: {OUTPUT_PREFIX}")
    log(f"Summary report: {report_path}")
    log("\n=== FINAL BEST-PER-INDEX (walk-forward Test) ===")
    for idx, best in best_per_index.items():
        if best:
            log(f"  {idx}: SL={best['hard_sl_pct']} TS={best['time_stop_minutes']} "
                f"Risk={best['risk_per_trade_pct']} Conf={best['min_confidence']} "
                f"-> return={best['return_pct']}%, consistency={best['consistency_score']}")
    log(f"\n=== COMBINED PORTFOLIO (all 3 indices' best, simultaneous) ===")
    log(f"  Total capital: Rs {summary_b['total_initial_capital']:,.0f} -> Rs {summary_b['total_final_capital']:,.0f}")
    log(f"  Combined return: {summary_b['combined_return_pct']}%")


if __name__ == "__main__":
    main()
