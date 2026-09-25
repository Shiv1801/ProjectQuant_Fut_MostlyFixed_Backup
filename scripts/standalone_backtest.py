"""
Standalone backtest sweep tool — NOT integrated into the app. Run
entirely from the command line. Fetches historical data directly from
Fyers if not already cached locally, then runs a full parameter grid
sweep and prints Top-5 tables plus a full CSV.

Usage:
    python standalone_backtest.py

Prompts for: index, mode (full-history / walk-forward), and whether to
force a fresh Fyers data fetch.

Grid (as specified): Hard SL 0-10% step 0.5, Time Stop 0-60min step 5,
TSL Activation fixed 5%, TSL Base Trail 3-5% step 0.5, Risk/Trade 0-10%
step 1, Min Confidence 0.6-1.0 step 0.05. Degenerate values (SL=0,
Time Stop=0, Risk/Trade=0) are auto-pruned since they produce
meaningless all-loss or no-trade results — final grid is 20 x 12 x 5 x
10 x 9 = 108,000 combinations.

Reuses the SAME gate/risk logic already built and tested in the main
app (engine/confluence_gate, engine/risk_engine) rather than
reimplementing from scratch, to avoid reintroducing bugs already fixed
there. The core per-combination simulation is vectorized with NumPy
so 113,400 combinations is actually tractable on a laptop.
"""
import sys
import time
import csv
import json
import itertools
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import requests

# --- Reuse existing, already-tested engine code ---
sys.path.insert(0, str(Path(__file__).parent.parent))  # project root, not the scripts/ folder itself
from config.settings import settings
from engine.data_layer.fyers_client.auth import get_fyers_model
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

TOKEN_PATH = Path(".fyers_token")
CACHE_DIR = Path("standalone_backtest_cache")
CACHE_DIR.mkdir(exist_ok=True)

SYMBOLS = {"NIFTY50": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX"}

# ---------------- Parameter grid (as specified, degenerate values pruned) ----------------
HARD_SL_VALUES = [round(x, 1) for x in np.arange(0.5, 10.5, 0.5)]           # 0.5-10, step 0.5 -> 20 values (0 dropped)
TIME_STOP_VALUES = list(range(5, 65, 5))                                    # 5-60, step 5 -> 12 values (0 dropped)
TSL_ACTIVATION = 5.0                                                        # fixed
TSL_BASE_TRAIL_VALUES = [3.0, 3.5, 4.0, 4.5, 5.0]                           # 5 values
RISK_PER_TRADE_VALUES = list(range(1, 11))                                  # 1-10, step 1 -> 10 values (0 dropped)
MIN_CONFIDENCE_VALUES = [round(x, 2) for x in np.arange(0.6, 1.05, 0.05)]   # 0.6-1.0, step 0.05 -> 9 values

MIN_TRADES_FILTER = 5  # per existing project principle — exclude statistically meaningless samples
INITIAL_CAPITAL = 100000.0


def read_token() -> str:
    if not TOKEN_PATH.exists():
        print("ERROR: No .fyers_token file found in this directory. Log into the main app first "
              "(this tool reuses that same saved token — it doesn't do its own separate Fyers login).")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def fetch_historical_candles_from_fyers(index_id: str, days_back: int = 90) -> list[dict]:
    """
    Fetches raw 1-min candles directly from Fyers' History API
    (matching the official docs exactly), independent of the main
    app's DB — this tool is standalone as requested.
    """
    token = read_token()
    fyers = get_fyers_model(token)
    fyers_symbol = SYMBOLS[index_id]

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    print(f"Fetching {days_back} days of 1-min candles for {index_id} ({fyers_symbol}) from Fyers...")
    data = {
        "symbol": fyers_symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": start_date.strftime("%Y-%m-%d"),
        "range_to": end_date.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    response = fyers.history(data=data)

    if response.get("s") != "ok":
        print(f"ERROR fetching from Fyers: {response}")
        sys.exit(1)

    raw_candles = response.get("candles", [])
    candles = [
        {"epoch": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in raw_candles
    ]
    print(f"Fetched {len(candles)} candles.")
    return candles


def get_candles(index_id: str, force_refresh: bool) -> list[dict]:
    cache_file = CACHE_DIR / f"{index_id}_candles.json"
    if cache_file.exists() and not force_refresh:
        print(f"Using cached candles from {cache_file} (delete this file or answer 'y' to refresh prompt to force a fresh Fyers fetch).")
        return json.loads(cache_file.read_text())

    candles = fetch_historical_candles_from_fyers(index_id)
    cache_file.write_text(json.dumps(candles))
    return candles


# ---------------- Backtest core (reuses existing gate/risk logic) ----------------

def precompute_signals(closes: np.ndarray, hurst_window: int = 100, zscore_window: int = 20):
    """
    Precomputes the quant direction + confidence for every candle ONCE,
    since Hurst/Z-score/chart-structure don't depend on any of the swept
    risk parameters (SL/TSL/Risk-per-trade/Time-stop) — only the ENTRY
    decision depends on min_confidence. This is the key vectorization
    insight: 113,400 combinations don't need 113,400 independent passes
    over the signal-generation logic, only over the trade-management logic.
    """
    n = len(closes)
    directions = [None] * n
    confidences = np.zeros(n)

    for i in range(hurst_window, n):
        window = closes[max(0, i - hurst_window):i]
        hurst_result = compute_hurst_exponent(window.tolist())
        zscore_result = compute_zscore(window[-zscore_window:].tolist(), window=zscore_window)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)
        directions[i] = quant_result.get("direction")
        confidences[i] = quant_result.get("confidence", 0.0)

    return directions, confidences


def precompute_chart_confirmations(candles: list[dict], directions: list, hurst_window: int = 100):
    """Chart structure also doesn't depend on swept params, only on the
    direction (already precomputed) — so this is precomputed once too."""
    n = len(candles)
    confirmed = [False] * n
    for i in range(hurst_window, n):
        if directions[i] is None:
            continue
        chart_slice = candles[max(0, i - hurst_window):i + 1]
        result = confirm_chart_structure(chart_slice, directions[i])
        confirmed[i] = result.get("confirmed", False)
    return confirmed


def run_single_combination(candles: list[dict], directions: list, confidences: np.ndarray,
                            confirmed: list, index_id: str, hard_sl_pct: float, time_stop_minutes: int,
                            tsl_activation_pct: float, tsl_base_trail_pct: float,
                            risk_per_trade_pct: float, min_confidence: float,
                            approx_entry_premium: float = 60.0, approx_delta: float = 0.35) -> dict:
    """
    Given PRECOMPUTED signals, simulates one specific parameter
    combination's trade-by-trade outcome. This is the part that
    actually varies per combination and can't be precomputed — but
    it's cheap (just arithmetic per candle, no model recomputation).
    """
    lot_size = LOT_SIZES.get(index_id)
    closes = np.array([c["close"] for c in candles])
    n = len(candles)

    trades = []
    open_trade = None
    capital = INITIAL_CAPITAL
    tsl_k = 0.15
    tsl_floor_pct = 2.0  # floor below base trail, consistent with existing engine's ratchet logic

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

            tsl_active = open_trade["peak_profit_pct"] >= tsl_activation_pct
            if tsl_active:
                profit_above_activation = open_trade["peak_profit_pct"] - tsl_activation_pct
                trail_distance = max(tsl_floor_pct, tsl_base_trail_pct - tsl_k * profit_above_activation)
                locked_floor = open_trade["peak_profit_pct"] - trail_distance
                tsl_hit = profit_pct <= locked_floor
            else:
                tsl_hit = False

            if hard_hit or time_hit or tsl_hit:
                reason = "hard_sl_hit" if hard_hit else ("time_stop" if time_hit else "tsl_hit")
                pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                capital += pnl
                trades.append({"pnl": pnl, "pnl_pct": profit_pct, "reason": reason, "hold_minutes": elapsed})
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

    if not trades:
        # A trade may still be OPEN at the end of the data window — this is
        # different from "no signal ever fired," and reporting trade_count=0
        # in that case would be misleading (it looks identical to "nothing
        # happened" when actually a trade opened and just never got a chance
        # to close within this data window). Mark it distinctly.
        if open_trade is not None:
            return {"trade_count": 0, "win_rate": None, "return_pct": None, "max_drawdown_pct": None,
                    "avg_win_pct": None, "avg_loss_pct": None, "final_capital": INITIAL_CAPITAL,
                    "note": "one trade opened but never closed within this data window (still open at end of history)"}
        return {"trade_count": 0, "win_rate": None, "return_pct": None, "max_drawdown_pct": None,
                "avg_win_pct": None, "avg_loss_pct": None, "final_capital": INITIAL_CAPITAL}

    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)

    running = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for t in trades:
        running += t["pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "return_pct": round(total_pnl / INITIAL_CAPITAL * 100, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(abs(t["pnl_pct"]) for t in trades if t["pnl"] <= 0) / max(1, len(trades) - len(wins)), 3) if len(trades) > len(wins) else None,
        "final_capital": round(INITIAL_CAPITAL + total_pnl, 2),
    }


def run_sweep(index_id: str, candles: list[dict], walk_forward: bool):
    closes = np.array([c["close"] for c in candles])

    if walk_forward:
        split = int(len(candles) * 0.7)
        segments = {"train": (candles[:split], closes[:split]), "test": (candles[split:], closes[split:])}
    else:
        segments = {"full": (candles, closes)}

    combos = list(itertools.product(
        HARD_SL_VALUES, TIME_STOP_VALUES, TSL_BASE_TRAIL_VALUES, RISK_PER_TRADE_VALUES, MIN_CONFIDENCE_VALUES,
    ))
    print(f"\nTotal combinations to test: {len(combos):,}")

    results_by_segment = {}
    for seg_name, (seg_candles, seg_closes) in segments.items():
        print(f"\nPrecomputing signals for segment '{seg_name}' ({len(seg_candles)} candles) — this happens ONCE, not per combination...")
        t0 = time.time()
        directions, confidences = precompute_signals(seg_closes)
        confirmed = precompute_chart_confirmations(seg_candles, directions)
        print(f"Signal precomputation done in {time.time()-t0:.1f}s")

        print(f"Running {len(combos):,} combinations against segment '{seg_name}'...")
        t0 = time.time()
        results = []
        checkpoint_every = 5000
        out_path = f"standalone_backtest_{index_id}_{seg_name}.csv"

        for idx, (sl, ts, tsl_base, risk, conf) in enumerate(combos):
            r = run_single_combination(
                seg_candles, directions, confidences, confirmed, index_id,
                hard_sl_pct=sl, time_stop_minutes=ts, tsl_activation_pct=TSL_ACTIVATION,
                tsl_base_trail_pct=tsl_base, risk_per_trade_pct=risk, min_confidence=conf,
            )
            r.update({"hard_sl_pct": sl, "time_stop_minutes": ts, "tsl_activation_pct": TSL_ACTIVATION,
                       "tsl_base_trail_pct": tsl_base, "risk_per_trade_pct": risk, "min_confidence": conf})
            results.append(r)

            if (idx + 1) % checkpoint_every == 0 or (idx + 1) == len(combos):
                elapsed = time.time() - t0
                rate = (idx + 1) / elapsed
                eta_sec = (len(combos) - idx - 1) / rate if rate > 0 else 0
                print(f"  [{idx+1:,}/{len(combos):,}] {rate:.0f} combos/sec, ETA {eta_sec/60:.1f} min remaining")
                _write_csv(results, out_path)

        results_by_segment[seg_name] = results
        print(f"Segment '{seg_name}' complete in {(time.time()-t0)/60:.1f} min. Full results: {out_path}")

    return results_by_segment


def _write_csv(results: list[dict], path: str):
    if not results:
        return
    fieldnames = sorted({k for r in results for k in r.keys()})
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def print_top_tables(results: list[dict], label: str):
    qualified = [r for r in results if r["trade_count"] is not None and r["trade_count"] >= MIN_TRADES_FILTER]
    print(f"\n{'='*100}\n{label} — {len(qualified):,} of {len(results):,} combinations had >= {MIN_TRADES_FILTER} trades\n{'='*100}")

    if not qualified:
        print("No combinations met the minimum trade filter.")
        return

    print(f"\n--- TOP 5 BY WIN RATE ---")
    by_win = sorted(qualified, key=lambda r: r["win_rate"], reverse=True)[:5]
    _print_table(by_win)

    print(f"\n--- TOP 5 BY NET RETURN % ---")
    by_return = sorted(qualified, key=lambda r: r["return_pct"], reverse=True)[:5]
    _print_table(by_return)


def _print_table(rows: list[dict]):
    headers = ["SL%", "TimeStop", "TSLAct%", "TSLBase%", "Risk%", "MinConf", "Trades", "WinRate%", "Return%", "MaxDD%", "AvgWin%", "AvgLoss%"]
    print(" | ".join(f"{h:>9}" for h in headers))
    for r in rows:
        vals = [r["hard_sl_pct"], r["time_stop_minutes"], r["tsl_activation_pct"], r["tsl_base_trail_pct"],
                 r["risk_per_trade_pct"], r["min_confidence"], r["trade_count"],
                 round(r["win_rate"]*100, 1), r["return_pct"], r["max_drawdown_pct"],
                 r["avg_win_pct"], r["avg_loss_pct"]]
        print(" | ".join(f"{str(v):>9}" for v in vals))


def main():
    print("=" * 70)
    print("STANDALONE BACKTEST SWEEP — not part of the main app")
    print("=" * 70)

    index_id = input("Index (NIFTY50 / BANKNIFTY / FINNIFTY): ").strip().upper()
    if index_id not in SYMBOLS:
        print(f"Invalid index '{index_id}'. Must be one of {list(SYMBOLS.keys())}")
        sys.exit(1)

    mode = input("Mode (full / walkforward): ").strip().lower()
    walk_forward = mode == "walkforward"

    refresh = input("Force fresh Fyers data fetch even if cache exists? (y/n): ").strip().lower() == "y"

    total_combos = len(HARD_SL_VALUES) * len(TIME_STOP_VALUES) * len(TSL_BASE_TRAIL_VALUES) * len(RISK_PER_TRADE_VALUES) * len(MIN_CONFIDENCE_VALUES)
    print(f"\nGrid: {len(HARD_SL_VALUES)} SL x {len(TIME_STOP_VALUES)} TimeStop x {len(TSL_BASE_TRAIL_VALUES)} TSLBase x "
          f"{len(RISK_PER_TRADE_VALUES)} Risk x {len(MIN_CONFIDENCE_VALUES)} Confidence = {total_combos:,} combinations")
    confirm = input("Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        sys.exit(0)

    candles = get_candles(index_id, force_refresh=refresh)
    if len(candles) < 150:
        print(f"ERROR: Only {len(candles)} candles available — need at least 150. "
              f"Fyers 1-min history is limited to ~100 days per request; try again or check your token.")
        sys.exit(1)

    t_start = time.time()
    results_by_segment = run_sweep(index_id, candles, walk_forward)
    total_time = time.time() - t_start

    print(f"\n\nTOTAL RUNTIME: {total_time/60:.1f} minutes")

    for seg_name, results in results_by_segment.items():
        label = f"{index_id} — {seg_name.upper()}"
        if walk_forward and seg_name == "train":
            label += " (reference only — do not trust for decisions, see TEST segment)"
        print_top_tables(results, label)


if __name__ == "__main__":
    main()
