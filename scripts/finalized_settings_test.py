"""
Finalized-Settings Full-Year Backtest — runs each ACTIVE index (mode !=
"off") through ~1 year of real historical data using its EXACT current
live settings (fetched live from /config/indices, not hardcoded), and
records every simulated trade in the SAME shape as the real Trade Log
table: Index, Strike, Direction, Lots, Entry Date, Entry Time, Entry ₹,
Exit Date, Exit Time, Exit ₹, Reason, P&L, P&L%, Hold (min).

This is a single, fixed run per index — not a parameter sweep. It
answers: "if these exact settings had been live for the past year,
what would the trade log actually look like?"

Reuses the same signal-generation and trade-management logic already
tested in standalone_backtest.py, so results are consistent with
everything validated there.

Usage:
    python scripts/finalized_settings_test.py
"""
import sys
import csv
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import requests

from config.settings import settings
from engine.data_layer.fyers_client.auth import get_fyers_model
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

TOKEN_PATH = Path(".fyers_token")
CACHE_DIR = Path("finalized_test_cache")
CACHE_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_PREFIX = f"finalized_test_{RUN_ID}_"

SYMBOLS = {"NIFTY50": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX"}
INDICES = list(SYMBOLS.keys())

HURST_WINDOW = 100
ZSCORE_WINDOW = 20
DAYS_BACK = 365
API_BASE = "http://127.0.0.1:8001"

# Trade Log column order — MUST match the real app's table exactly.
TRADE_LOG_HEADERS = [
    "Index", "Strike", "Direction", "Lots", "Entry Date", "Entry Time", "Entry ₹",
    "Exit Date", "Exit Time", "Exit ₹", "Reason", "P&L", "P&L %", "Hold (min)",
]


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def fetch_live_index_configs() -> dict:
    """Pulls the EXACT current settings from the running app — never
    hardcoded here, so this test always reflects whatever is actually
    configured live, including if you change settings again later."""
    try:
        resp = requests.get(f"{API_BASE}/config/indices", timeout=5)
        payload = resp.json()
        if payload.get("status") != "ok":
            raise RuntimeError(payload)
        return payload["indices"]
    except requests.exceptions.RequestException as e:
        log(f"ERROR: could not reach the running app at {API_BASE} to fetch live settings: {e}")
        log("Make sure uvicorn is running before running this test.")
        sys.exit(1)


def read_token() -> str:
    if not TOKEN_PATH.exists():
        log("ERROR: No .fyers_token file found.")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def fetch_one_year_candles(index_id: str) -> list[dict]:
    token = read_token()
    fyers = get_fyers_model(token)
    fyers_symbol = SYMBOLS[index_id]

    end_date = datetime.now()
    all_candles = []
    chunk_days = 90
    remaining_days = DAYS_BACK
    chunk_end = end_date

    log(f"Fetching {DAYS_BACK} days of 1-min candles for {index_id}...")
    while remaining_days > 0:
        this_chunk = min(chunk_days, remaining_days)
        chunk_start = chunk_end - timedelta(days=this_chunk)
        data = {
            "symbol": fyers_symbol, "resolution": "1", "date_format": "1",
            "range_from": chunk_start.strftime("%Y-%m-%d"), "range_to": chunk_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        response = fyers.history(data=data)
        if response.get("s") == "ok":
            chunk_candles = [
                {"epoch": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
                for c in response.get("candles", [])
            ]
            all_candles = chunk_candles + all_candles
            log(f"  Fetched {len(chunk_candles)} candles for {chunk_start.date()} to {chunk_end.date()}")
        else:
            log(f"  WARNING: chunk failed: {response}")
        chunk_end = chunk_start
        remaining_days -= this_chunk
        time.sleep(0.5)

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


def epoch_to_ist_datestr_timestr(epoch: int) -> tuple[str, str]:
    """Fyers epochs are already IST-aligned in the historical API (no
    extra timezone conversion needed, matching how the rest of the app
    stores 'minute_bucket' as local IST strings)."""
    dt = datetime.fromtimestamp(epoch)
    return dt.strftime("%d %b %y"), dt.strftime("%I:%M %p")


def run_finalized_backtest(index_id: str, candles: list[dict], risk_params: dict) -> list[dict]:
    """
    Single fixed-parameter run (not a sweep) using the index's exact
    live settings. Records every trade in the exact Trade Log shape,
    including realistic strike selection (nearest round strike to the
    simulated index level, matching how strikes are actually quoted)
    and a constant-delta premium approximation — same documented
    limitation as the rest of our backtest tooling (real historical
    option-chain data isn't available, only index candles).
    """
    lot_size = LOT_SIZES.get(index_id)
    strike_step = 50 if index_id == "NIFTY50" else 100  # realistic strike spacing per index
    closes = np.array([c["close"] for c in candles])
    n = len(closes)

    directions, confidences = precompute_signals(closes)
    confirmed = precompute_chart_confirmations(candles, directions)

    hard_sl_pct = risk_params["hard_sl_pct"]
    time_stop_minutes = risk_params["time_stop_minutes"]
    tsl_activation_pct = risk_params["tsl_activation_pct"]
    tsl_base_trail_pct = risk_params["tsl_base_trail_pct"]
    risk_per_trade_pct = risk_params["risk_per_trade_pct"]
    min_confidence = risk_params["min_confluence_confidence"]
    tsl_k = 0.15
    tsl_floor_pct = 5.0

    capital = 50000.0  # matches the real app's per-index capital allocation
    approx_entry_premium = 60.0
    approx_delta = 0.35

    trades = []
    open_trade = None

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

                entry_date, entry_time = epoch_to_ist_datestr_timestr(open_trade["entry_epoch"])
                exit_date, exit_time = epoch_to_ist_datestr_timestr(candles[i].get("epoch", 0))

                trades.append({
                    "Index": index_id, "Strike": open_trade["strike"],
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

        sizing = compute_position_size(
            available_capital=capital, index_id=index_id, premium_per_share=approx_entry_premium,
            hard_sl_pct=hard_sl_pct, risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue

        strike = round(current_price / strike_step) * strike_step

        open_trade = {
            "entry_index": i, "entry_epoch": candles[i].get("epoch", 0), "entry_index_price": current_price,
            "entry_premium": approx_entry_premium, "direction": direction, "lots": sizing["lots"],
            "peak_profit_pct": 0.0, "strike": strike,
        }

    return trades


def print_trade_table(trades: list[dict], index_id: str):
    if not trades:
        print(f"\n{index_id}: no trades generated with these settings over this period.")
        return

    print(f"\n{index_id} — {len(trades)} trades")
    print(" | ".join(f"{h:>10}" for h in TRADE_LOG_HEADERS))
    for t in trades:
        print(" | ".join(f"{str(t[h]):>10}" for h in TRADE_LOG_HEADERS))

    wins = [t for t in trades if t["P&L"] > 0]
    total_pnl = sum(t["P&L"] for t in trades)
    print(f"\n  Summary: {len(trades)} trades, win rate {len(wins)/len(trades)*100:.1f}%, total P&L {total_pnl:,.2f}")


def write_trade_csv(trades: list[dict], index_id: str):
    if not trades:
        return
    path = f"{OUTPUT_PREFIX}{index_id}_trade_log.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
        writer.writeheader()
        writer.writerows(trades)
    log(f"  Wrote {path}")


def main():
    log("=" * 70)
    log("FINALIZED-SETTINGS FULL-YEAR TEST — using live /config/indices settings")
    log("=" * 70)

    live_configs = fetch_live_index_configs()
    log("\nLive settings fetched from the running app:")
    for idx in INDICES:
        cfg = live_configs.get(idx, {})
        log(f"  {idx}: mode={cfg.get('mode')}, {cfg.get('risk_params')}")

    refresh = input("\nForce fresh Fyers data fetch even if cache exists? (y/n): ").strip().lower() == "y"
    confirm = input("Proceed with the full 1-year test on all ACTIVE (non-Off) indices? (y/n): ").strip().lower()
    if confirm != "y":
        log("Cancelled.")
        return

    all_trades_combined = []

    for index_id in INDICES:
        cfg = live_configs.get(index_id)
        if not cfg or cfg.get("mode") == "off":
            log(f"\n{index_id}: mode is Off — SKIPPED (not tested, as intended).")
            continue

        log(f"\n{'='*70}\n{index_id} (mode={cfg['mode']})\n{'='*70}")
        candles = get_candles(index_id, force_refresh=refresh)
        if len(candles) < HURST_WINDOW + 20:
            log(f"  SKIPPING: only {len(candles)} candles, insufficient.")
            continue

        trades = run_finalized_backtest(index_id, candles, cfg["risk_params"])
        print_trade_table(trades, index_id)
        write_trade_csv(trades, index_id)
        all_trades_combined.extend(trades)

    if all_trades_combined:
        combined_path = f"{OUTPUT_PREFIX}ALL_INDICES_trade_log.csv"
        with open(combined_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
            writer.writeheader()
            writer.writerows(sorted(all_trades_combined, key=lambda t: (t["Entry Date"], t["Entry Time"])))
        log(f"\nCombined trade log (all active indices, chronological): {combined_path}")

    log(f"\nDone. All output files prefixed with: {OUTPUT_PREFIX}")


if __name__ == "__main__":
    main()
