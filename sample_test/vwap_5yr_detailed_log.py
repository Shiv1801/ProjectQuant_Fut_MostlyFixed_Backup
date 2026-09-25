"""
5-Year Nifty VWAP-Filtered Strategy — Complete Detailed Trade Log with
Real Charges Deducted.

Runs the confluence gate + VWAP filter strategy (the one that showed
the most balanced-looking result among the three tested: 587.5% gross
return on the 3-year test) across a full 5 YEARS of real Nifty data,
using your exact live settings, with every trade's REAL Fyers charges
(brokerage, STT, exchange fees, SEBI fee, stamp duty, GST — sourced
directly from Fyers' official published rates) deducted to show true
net P&L, not just gross.

Output columns extend the standard 14-column Trade Log format with
three additional columns: Gross P&L, Charges, and Net P&L — so you can
see exactly how much of the gross return charges actually eat into.

Usage:
    python scripts/vwap_5yr_detailed_log.py

Requires uvicorn running (fetches your live Nifty settings). Fetches
5 years of fresh 1-min candle data from Fyers directly (does NOT reuse
the 3-year cache, since this needs a longer history) — this WILL take
longer than previous scripts; expect several minutes for the fetch
alone given ~5x the data of the 1-year tools.

Output written to sample_test/ in the project root.
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

from engine.data_layer.fyers_client.auth import get_fyers_model
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.signal_layer.volatility.realized_vol import compute_realized_vol
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES
from engine.risk_engine.stops import check_session_end

sys.path.insert(0, str(Path(__file__).parent))
from premium_model import black_scholes_premium, days_to_next_weekly_expiry
from fyers_charges import compute_round_trip_charges

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_PATH = Path(".fyers_token")
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")
OUTPUT_DIR = Path("sample_test")
API_BASE = "http://127.0.0.1:8001"
INDEX_ID = "NIFTY50"
SYMBOL = "NSE:NIFTY50-INDEX"
STRIKE_STEP = 50
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
INITIAL_CAPITAL = 50000.0
DAYS_BACK = 5 * 365  # ~5 years

TRADE_LOG_HEADERS = [
    "Index", "Strike", "Direction", "Lots", "Entry Date", "Entry Time", "Entry ₹",
    "Exit Date", "Exit Time", "Exit ₹", "Reason", "Gross P&L", "Charges", "Net P&L",
    "P&L %", "Hold (min)",
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


def read_token() -> str:
    if not TOKEN_PATH.exists():
        log("ERROR: No .fyers_token file found.")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def fetch_five_years_candles() -> list[dict]:
    token = read_token()
    fyers = get_fyers_model(token)

    end_date = datetime.now()
    all_candles = []
    chunk_days = 90
    remaining_days = DAYS_BACK
    chunk_end = end_date

    log(f"Fetching {DAYS_BACK} days (~5 years) of 1-min candles for {INDEX_ID}...")
    log("This chains many 90-day requests -- will take several minutes.")
    chunk_num = 0
    while remaining_days > 0:
        chunk_num += 1
        this_chunk = min(chunk_days, remaining_days)
        chunk_start = chunk_end - timedelta(days=this_chunk)
        data = {
            "symbol": SYMBOL, "resolution": "1", "date_format": "1",
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
            log(f"  [chunk {chunk_num}] {chunk_start.date()} to {chunk_end.date()}: {len(chunk_candles)} candles"
                f"{' (EMPTY -- likely no data this far back)' if len(chunk_candles) == 0 else ''}")
        else:
            log(f"  [chunk {chunk_num}] WARNING: failed: {response}")
        chunk_end = chunk_start
        remaining_days -= this_chunk
        time.sleep(0.5)

    log(f"\nTotal candles fetched: {len(all_candles)}")
    if all_candles:
        first_date = datetime.fromtimestamp(all_candles[0]["epoch"], tz=IST)
        last_date = datetime.fromtimestamp(all_candles[-1]["epoch"], tz=IST)
        log(f"Actual date range received: {first_date.date()} to {last_date.date()}")
    return all_candles


def get_candles(force_refresh: bool) -> list[dict]:
    CACHE_FILE.parent.mkdir(exist_ok=True)
    if CACHE_FILE.exists() and not force_refresh:
        log(f"Using cached candles from {CACHE_FILE}")
        return json.loads(CACHE_FILE.read_text())
    candles = fetch_five_years_candles()
    CACHE_FILE.write_text(json.dumps(candles))
    return candles


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


def epoch_to_ist_datestr_timestr(epoch: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(epoch, tz=IST)
    return dt.strftime("%d %b %y"), dt.strftime("%I:%M %p")


def run_vwap_filtered_simulation(candles: list[dict], directions: list, confidences: np.ndarray,
                                  confirmed: list, risk_params: dict, vwap_series: list, closes_list: list,
                                  use_vwap_filter: bool = True, strikes_tracked_per_side: int = 10) -> list[dict]:
    """
    use_vwap_filter: when True (default), only enters a trade if VWAP
    agrees with the gate's direction (the strategy under test). When
    False, runs the confluence gate ALONE with no VWAP filter — the
    baseline for direct comparison, so both runs share every other
    piece of logic (premium model, sizing, ALL exit rules including the
    new session-end / strike-scope rules) and differ ONLY in whether
    the VWAP filter gates entries.

    strikes_tracked_per_side: mirrors the live app's new configurable
    account setting of the same name (default 10 -> 21 total strikes
    tracked). Used here to enforce the new strike-scope exit rule
    faithfully in this standalone backtest: a position is "in scope" if
    its strike sits within strikes_tracked_per_side * STRIKE_STEP of
    the CURRENT spot at each check -- the same centering logic the live
    option-chain poller uses (ATM +/- N strikes).
    """
    lot_size = LOT_SIZES.get(INDEX_ID)
    closes = np.array(closes_list)
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

            # NEW RULE (session end): force-exit if within 2 min of
            # 15:30 close, regardless of profit/loss. Same function,
            # same 2-min buffer, as the live app's new exit rule.
            session_end_check = check_session_end(current_dt)
            session_end_hit = session_end_check["triggered"]

            # NEW RULE (strike scope): force-exit if the position's
            # strike has drifted outside the currently-tracked range
            # (ATM +/- strikes_tracked_per_side * STRIKE_STEP), mirroring
            # the live app's new strike-scope exit rule and its
            # configurable strikes_tracked_per_side setting.
            current_atm = round(current_price / STRIKE_STEP) * STRIKE_STEP
            scope_half_width = strikes_tracked_per_side * STRIKE_STEP
            strike_out_of_scope = abs(open_trade["strike"] - current_atm) > scope_half_width

            if session_end_hit or strike_out_of_scope or hard_hit or time_hit or tsl_hit:
                if session_end_hit:
                    reason = "session_end_exit"
                elif strike_out_of_scope:
                    reason = "strike_out_of_scope"
                elif hard_hit:
                    reason = "hard_sl_hit"
                elif time_hit:
                    reason = "time_stop"
                else:
                    reason = "tsl_hit"
                gross_pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size

                charges = compute_round_trip_charges(
                    entry_premium=open_trade["entry_premium"], exit_premium=sim_premium,
                    lots=open_trade["lots"], lot_size=lot_size,
                )
                net_pnl = gross_pnl - charges["total_charges"]
                capital += net_pnl  # capital compounds on NET P&L, the real, honest amount

                entry_date, entry_time = epoch_to_ist_datestr_timestr(open_trade["entry_epoch"])
                exit_date, exit_time = epoch_to_ist_datestr_timestr(candles[i].get("epoch", 0))

                trades.append({
                    "Index": INDEX_ID, "Strike": open_trade["strike"],
                    "Direction": "BULLISH" if open_trade["direction"] == "long_call" else "BEARISH",
                    "Lots": open_trade["lots"],
                    "Entry Date": entry_date, "Entry Time": entry_time, "Entry ₹": round(open_trade["entry_premium"], 2),
                    "Exit Date": exit_date, "Exit Time": exit_time, "Exit ₹": round(sim_premium, 2),
                    "Reason": reason, "Gross P&L": round(gross_pnl, 2), "Charges": charges["total_charges"],
                    "Net P&L": round(net_pnl, 2), "P&L %": round(profit_pct, 2), "Hold (min)": elapsed,
                })
                open_trade = None
            continue

        direction = directions[i]
        if direction is None or confidences[i] < min_confidence or not confirmed[i]:
            continue

        if use_vwap_filter:
            if vwap_series[i] is None:
                continue
            vwap_signal = "bullish" if closes_list[i] > vwap_series[i] else "bearish"
            gate_signal = "bullish" if direction == "long_call" else "bearish"
            if vwap_signal != gate_signal:
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
        if sizing["lots"] < 1 or sizing["lots"] > 500:
            continue

        open_trade = {
            "entry_index": i, "entry_epoch": candles[i].get("epoch", 0), "entry_index_price": current_price,
            "entry_premium": entry_premium, "direction": direction, "lots": sizing["lots"],
            "peak_profit_pct": 0.0, "strike": strike,
        }

    return trades


def main():
    log("=" * 70)
    log("5-YEAR NIFTY VWAP-FILTERED STRATEGY -- detailed trade log with real charges")
    log("=" * 70)

    OUTPUT_DIR.mkdir(exist_ok=True)
    nifty_config = fetch_live_nifty_config()
    risk_params = nifty_config["risk_params"]
    log(f"\nUsing live Nifty settings: {risk_params}")

    refresh = input("\nForce fresh Fyers data fetch even if cache exists? (y/n): ").strip().lower() == "y"
    confirm = input("Proceed with ~5 years of data fetch + VWAP strategy simulation? This will take several minutes. (y/n): ").strip().lower()
    if confirm != "y":
        log("Cancelled.")
        return

    candles = get_candles(force_refresh=refresh)
    if len(candles) < HURST_WINDOW + 100:
        log(f"ERROR: only {len(candles)} candles available, need substantially more.")
        sys.exit(1)

    closes = np.array([c["close"] for c in candles])
    closes_list = closes.tolist()

    log("\nPrecomputing confluence-gate signals across the full 5-year dataset...")
    t0 = time.time()
    directions, confidences = precompute_gate_signals(closes)
    confirmed = precompute_chart_confirmations(candles, directions)
    log(f"Done in {time.time()-t0:.1f}s")

    log("Computing VWAP (resets each real trading day)...")
    vwap_series = compute_vwap_series(candles)

    strikes_per_side = 10  # matches the live app's default; change here if you've updated the Settings field

    def run_and_report(use_vwap: bool, label: str, filename: str):
        log(f"\n--- Running: {label} (session-end + strike-scope rules ACTIVE in both runs) ---")
        trades = run_vwap_filtered_simulation(
            candles, directions, confidences, confirmed, risk_params, vwap_series, closes_list,
            use_vwap_filter=use_vwap, strikes_tracked_per_side=strikes_per_side,
        )
        out_path = OUTPUT_DIR / filename
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
            writer.writeheader()
            writer.writerows(trades)
        log(f"  Wrote {out_path} ({len(trades)} trades)")

        if not trades:
            print(f"\n{label}: no trades generated.")
            return None

        wins = [t for t in trades if t["Net P&L"] > 0]
        total_gross = sum(t["Gross P&L"] for t in trades)
        total_charges = sum(t["Charges"] for t in trades)
        total_net = sum(t["Net P&L"] for t in trades)
        final_capital = INITIAL_CAPITAL + total_net

        # Exit-reason breakdown -- useful to see how often the two NEW
        # rules actually fire, distinct from the pre-existing ones.
        from collections import Counter
        reason_counts = Counter(t["Reason"] for t in trades)

        summary = {
            "label": label, "trades": len(trades), "win_rate": round(len(wins)/len(trades)*100, 1),
            "total_gross": round(total_gross, 2), "total_charges": round(total_charges, 2),
            "total_net": round(total_net, 2), "final_capital": round(final_capital, 2),
            "gross_return_pct": round(total_gross/INITIAL_CAPITAL*100, 1),
            "net_return_pct": round(total_net/INITIAL_CAPITAL*100, 1),
            "exit_reasons": dict(reason_counts),
        }

        print(f"\n{label}:")
        print(f"  Trades: {summary['trades']}, Win rate: {summary['win_rate']}%")
        print(f"  Gross P&L: Rs {summary['total_gross']:,.2f} | Charges: Rs {summary['total_charges']:,.2f} | "
              f"Net P&L: Rs {summary['total_net']:,.2f}")
        print(f"  Final capital: Rs {summary['final_capital']:,.2f}")
        print(f"  Gross return: {summary['gross_return_pct']}% | NET return: {summary['net_return_pct']}%")
        print(f"  Exit reasons: {summary['exit_reasons']}")
        return summary

    summary_with_vwap = run_and_report(True, "WITH VWAP filter", "vwap_5yr_WITH_vwap_trade_log.csv")
    summary_without_vwap = run_and_report(False, "WITHOUT VWAP filter (gate alone)", "vwap_5yr_WITHOUT_vwap_trade_log.csv")

    print("\n" + "=" * 70)
    print("SIDE-BY-SIDE COMPARISON (both include the new session-end + strike-scope exit rules)")
    print("=" * 70)
    if summary_with_vwap and summary_without_vwap:
        print(f"{'Metric':<20} {'WITH VWAP':<20} {'WITHOUT VWAP'}")
        print("-" * 60)
        print(f"{'Trades':<20} {summary_with_vwap['trades']:<20} {summary_without_vwap['trades']}")
        print(f"{'Win Rate %':<20} {summary_with_vwap['win_rate']:<20} {summary_without_vwap['win_rate']}")
        print(f"{'Net P&L (Rs)':<20} {summary_with_vwap['total_net']:<20,.2f} {summary_without_vwap['total_net']:,.2f}")
        print(f"{'Net Return %':<20} {summary_with_vwap['net_return_pct']:<20} {summary_without_vwap['net_return_pct']}")

    combined_summary = {"with_vwap": summary_with_vwap, "without_vwap": summary_without_vwap}
    with open(OUTPUT_DIR / "vwap_5yr_comparison_summary.json", "w", encoding="utf-8") as f:
        json.dump(combined_summary, f, indent=2)
    log(f"\nComparison summary written to {OUTPUT_DIR / 'vwap_5yr_comparison_summary.json'}")


if __name__ == "__main__":
    main()
