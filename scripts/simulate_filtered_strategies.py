"""
Simulated Strategy Trade Logs — runs THREE actual filtered strategies
(not just re-tagging existing trades) through the real 3-year Nifty
candle history:
  1) Confluence Gate + EMA 10/20 filter (only trade when EMA agrees)
  2) Confluence Gate + VWAP filter (only trade when VWAP agrees)
  3) Confluence Gate + Ichimoku Tenkan/Kijun filter (only trade when it agrees)

Each starts with its OWN independent Rs 50,000 capital, uses the real
Black-Scholes premium model (same as nifty_mastery_test.py, same IST-
aware timezone handling, same 500-lot safety cap — all previously
found and fixed bugs), and produces a genuine trade-by-trade simulation
with real compounding, not a post-hoc summary.

Output: one CSV per strategy, in the EXACT SAME 14-column format as
the real app's Trade Log table, written to sample_test/.

Usage:
    python scripts/simulate_filtered_strategies.py

Requires the cached candles already fetched by nifty_mastery_test.py
(nifty_mastery_cache/nifty_3yr_candles.json) and your live Nifty
settings (fetched fresh from the running app, same as
nifty_mastery_test.py — uvicorn must be running).
"""
import sys
import csv
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.signal_layer.volatility.realized_vol import compute_realized_vol
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

sys.path.insert(0, str(Path(__file__).parent))
from premium_model import black_scholes_premium, days_to_next_weekly_expiry

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("nifty_mastery_cache/nifty_3yr_candles.json")
OUTPUT_DIR = Path("sample_test")
API_BASE = "http://127.0.0.1:8001"
INDEX_ID = "NIFTY50"
STRIKE_STEP = 50
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
INITIAL_CAPITAL = 50000.0

TRADE_LOG_HEADERS = [
    "Index", "Strike", "Direction", "Lots", "Entry Date", "Entry Time", "Entry ₹",
    "Exit Date", "Exit Time", "Exit ₹", "Reason", "P&L", "P&L %", "Hold (min)",
]


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def fetch_live_nifty_config() -> dict:
    try:
        resp = requests.get(f"{API_BASE}/config/indices/NIFTY50", timeout=5)
        payload = resp.json()
        if payload.get("status") != "ok":
            raise RuntimeError(payload)
        return payload["config"]
    except requests.exceptions.RequestException as e:
        log(f"ERROR: could not reach the running app at {API_BASE}: {e}")
        log("Make sure uvicorn is running before running this script.")
        sys.exit(1)


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run nifty_mastery_test.py first.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        candles = json.load(f)
    log(f"Loaded {len(candles)} cached candles.")
    return candles


# ---------------- Shared signal precomputation (reused across all 3 strategies) ----------------

def precompute_gate_signals(closes: np.ndarray):
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


def compute_ema_series(closes: list[float], period: int) -> list[float | None]:
    n = len(closes)
    ema = [None] * n
    if n < period:
        return ema
    multiplier = 2 / (period + 1)
    sma = sum(closes[:period]) / period
    ema[period - 1] = sma
    for i in range(period, n):
        ema[i] = (closes[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def compute_vwap_series(candles: list[dict]) -> list[float | None]:
    n = len(candles)
    vwap = [None] * n
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    current_day = None
    for i, c in enumerate(candles):
        candle_day = datetime.fromtimestamp(c["epoch"], tz=IST).date()
        if candle_day != current_day:
            current_day = candle_day
            cumulative_pv = 0.0
            cumulative_vol = 0.0
        volume = c.get("volume", 0) or 0
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        cumulative_pv += typical_price * volume
        cumulative_vol += volume
        vwap[i] = (cumulative_pv / cumulative_vol) if cumulative_vol > 0 else None
    return vwap


def compute_midpoint_line(candles: list[dict], period: int) -> list[float | None]:
    n = len(candles)
    line = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]
        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        line[i] = (highest + lowest) / 2
    return line


def epoch_to_ist_datestr_timestr(epoch: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(epoch, tz=IST)
    return dt.strftime("%d %b %y"), dt.strftime("%I:%M %p")


# ---------------- Core simulation, parameterized by an optional extra filter ----------------

def run_filtered_simulation(candles: list[dict], directions: list, confidences: np.ndarray,
                             confirmed: list, risk_params: dict, filter_check) -> list[dict]:
    """
    filter_check(i) -> True/False/None: called at each candidate entry
    candle index. True = filter agrees with the gate's direction (take
    the trade), False = filter disagrees (skip), None = filter has no
    opinion yet (skip, insufficient data for that indicator).

    Everything else (premium model, position sizing, SL/TSL/time-stop,
    500-lot safety cap, IST-aware expiry) is IDENTICAL to
    nifty_mastery_test.py's proven, tested simulation logic.
    """
    lot_size = LOT_SIZES.get(INDEX_ID)
    closes = np.array([c["close"] for c in candles])
    n = len(closes)

    hard_sl_pct = risk_params["hard_sl_pct"]
    time_stop_minutes = risk_params["time_stop_minutes"]
    tsl_activation_pct = risk_params["tsl_activation_pct"]
    tsl_base_trail_pct = risk_params["tsl_base_trail_pct"]
    risk_per_trade_pct = risk_params["risk_per_trade_pct"]
    min_confidence = risk_params["min_confluence_confidence"]
    tsl_k = 0.15
    tsl_floor_pct = 5.0

    capital = INITIAL_CAPITAL
    vol_window = 100

    trades = []
    open_trade = None

    for i in range(n):
        current_price = closes[i]
        current_epoch = candles[i].get("epoch", 0)
        current_dt = datetime.fromtimestamp(current_epoch, tz=IST) if current_epoch else datetime.now(tz=IST)

        vol_window_closes = closes[max(0, i - vol_window):i + 1].tolist()
        rv_result = compute_realized_vol(vol_window_closes) if len(vol_window_closes) >= 20 else {}
        current_vol_pct = rv_result.get("realized_vol_pct") or 12.0

        if open_trade is not None:
            days_left = days_to_next_weekly_expiry(current_dt)
            option_type = "CE" if open_trade["direction"] == "long_call" else "PE"
            sim_premium = black_scholes_premium(
                spot=current_price, strike=open_trade["strike"], days_to_expiry=days_left,
                volatility_pct=current_vol_pct, option_type=option_type,
            )
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

                entry_date, entry_time = epoch_to_ist_datestr_timestr(open_trade["entry_epoch"])
                exit_date, exit_time = epoch_to_ist_datestr_timestr(candles[i].get("epoch", 0))

                trades.append({
                    "Index": INDEX_ID, "Strike": open_trade["strike"],
                    "Direction": "BULLISH" if open_trade["direction"] == "long_call" else "BEARISH",
                    "Lots": open_trade["lots"],
                    "Entry Date": entry_date, "Entry Time": entry_time, "Entry ₹": round(open_trade["entry_premium"], 2),
                    "Exit Date": exit_date, "Exit Time": exit_time, "Exit ₹": round(sim_premium, 2),
                    "Reason": reason, "P&L": round(pnl, 2), "P&L %": round(profit_pct, 2), "Hold (min)": elapsed,
                })
                open_trade = None
            continue

        direction = directions[i]
        if direction is None or confidences[i] < min_confidence or not confirmed[i]:
            continue

        # THE FILTER: only take the trade if the extra indicator agrees
        # with the gate's direction. This is what makes each script a
        # genuinely different, real strategy -- not a re-tag of the
        # unfiltered trade log.
        filter_result = filter_check(i)
        if filter_result is not True:
            continue

        strike = round(current_price / STRIKE_STEP) * STRIKE_STEP
        option_type = "CE" if direction == "long_call" else "PE"
        days_left = days_to_next_weekly_expiry(current_dt)
        entry_premium = black_scholes_premium(
            spot=current_price, strike=strike, days_to_expiry=days_left,
            volatility_pct=current_vol_pct, option_type=option_type,
        )

        sizing = compute_position_size(
            available_capital=capital, index_id=INDEX_ID, premium_per_share=entry_premium,
            hard_sl_pct=hard_sl_pct, risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue
        if sizing["lots"] > 500:  # same defense-in-depth cap as nifty_mastery_test.py
            continue

        open_trade = {
            "entry_index": i, "entry_epoch": candles[i].get("epoch", 0), "entry_index_price": current_price,
            "entry_premium": entry_premium, "direction": direction, "lots": sizing["lots"],
            "peak_profit_pct": 0.0, "strike": strike,
        }

    return trades


def write_trade_csv(trades: list[dict], filename: str):
    path = OUTPUT_DIR / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
        writer.writeheader()
        writer.writerows(trades)
    log(f"  Wrote {path} ({len(trades)} trades)")
    return path


def summarize(trades: list[dict], label: str):
    if not trades:
        print(f"\n{label}: no trades generated.")
        return
    wins = [t for t in trades if t["P&L"] > 0]
    total_pnl = sum(t["P&L"] for t in trades)
    final_capital = INITIAL_CAPITAL + total_pnl
    print(f"\n{label}:")
    print(f"  Starting capital: Rs {INITIAL_CAPITAL:,.2f}")
    print(f"  Trades: {len(trades)}")
    print(f"  Win rate: {len(wins)/len(trades)*100:.1f}%")
    print(f"  Total P&L: Rs {total_pnl:,.2f}")
    print(f"  Final capital: Rs {final_capital:,.2f}")
    print(f"  Return on initial capital: {total_pnl/INITIAL_CAPITAL*100:.1f}%")


def main():
    log("=" * 70)
    log("SIMULATED FILTERED STRATEGIES — real trade-by-trade capital tracking")
    log("Each strategy starts fresh with its OWN Rs 50,000 capital")
    log("=" * 70)

    OUTPUT_DIR.mkdir(exist_ok=True)

    nifty_config = fetch_live_nifty_config()
    risk_params = nifty_config["risk_params"]
    log(f"\nUsing live Nifty settings: {risk_params}")

    candles = load_candles()
    closes = np.array([c["close"] for c in candles])

    log("\nPrecomputing shared confluence-gate signals (one pass, reused by all 3 strategies)...")
    t0 = time.time()
    directions, confidences = precompute_gate_signals(closes)
    confirmed = precompute_chart_confirmations(candles, directions)
    log(f"Done in {time.time()-t0:.1f}s")

    closes_list = closes.tolist()

    # ---- Strategy 1: Gate + EMA 10/20 filter ----
    log("\n--- Strategy 1: Confluence Gate + EMA 10/20 filter ---")
    fast_ema = compute_ema_series(closes_list, 10)
    slow_ema = compute_ema_series(closes_list, 20)

    def ema_filter(i):
        if fast_ema[i] is None or slow_ema[i] is None:
            return None
        ema_signal = "bullish" if fast_ema[i] > slow_ema[i] else "bearish"
        gate_signal = "bullish" if directions[i] == "long_call" else "bearish"
        return ema_signal == gate_signal

    ema_trades = run_filtered_simulation(candles, directions, confidences, confirmed, risk_params, ema_filter)
    write_trade_csv(ema_trades, "strategy_ema_filter_trade_log.csv")
    summarize(ema_trades, "STRATEGY 1: Gate + EMA 10/20 Filter")

    # ---- Strategy 2: Gate + VWAP filter ----
    log("\n--- Strategy 2: Confluence Gate + VWAP filter ---")
    vwap_series = compute_vwap_series(candles)

    def vwap_filter(i):
        if vwap_series[i] is None:
            return None
        vwap_signal = "bullish" if closes_list[i] > vwap_series[i] else "bearish"
        gate_signal = "bullish" if directions[i] == "long_call" else "bearish"
        return vwap_signal == gate_signal

    vwap_trades = run_filtered_simulation(candles, directions, confidences, confirmed, risk_params, vwap_filter)
    write_trade_csv(vwap_trades, "strategy_vwap_filter_trade_log.csv")
    summarize(vwap_trades, "STRATEGY 2: Gate + VWAP Filter")

    # ---- Strategy 3: Gate + Ichimoku Tenkan/Kijun filter ----
    log("\n--- Strategy 3: Confluence Gate + Ichimoku Tenkan/Kijun filter ---")
    tenkan = compute_midpoint_line(candles, 9)
    kijun = compute_midpoint_line(candles, 26)

    def ichimoku_filter(i):
        if tenkan[i] is None or kijun[i] is None:
            return None
        tk_signal = "bullish" if tenkan[i] > kijun[i] else "bearish"
        gate_signal = "bullish" if directions[i] == "long_call" else "bearish"
        return tk_signal == gate_signal

    ichimoku_trades = run_filtered_simulation(candles, directions, confidences, confirmed, risk_params, ichimoku_filter)
    write_trade_csv(ichimoku_trades, "strategy_ichimoku_filter_trade_log.csv")
    summarize(ichimoku_trades, "STRATEGY 3: Gate + Ichimoku Tenkan/Kijun Filter")

    # ---- Baseline for comparison: Gate alone, no extra filter ----
    log("\n--- Baseline: Confluence Gate alone (no extra filter, for comparison) ---")
    baseline_trades = run_filtered_simulation(candles, directions, confidences, confirmed, risk_params, lambda i: True)
    write_trade_csv(baseline_trades, "strategy_baseline_no_filter_trade_log.csv")
    summarize(baseline_trades, "BASELINE: Gate Alone (no filter)")

    log(f"\nAll 4 trade logs written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
