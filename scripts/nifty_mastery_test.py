"""
Nifty Mastery Test — fetches ~3 years of real 1-min Nifty candles and
produces:
  1) FULL trade log (all 3 years) using current live Nifty settings,
     in the EXACT same format as the app's real Trade Log table.
  2) WALK-FORWARD trade log: trained conceptually on the first 2 years
     (used only for computing signals, which are parameter-free — the
     actual settings are your already-finalized live ones, so "train"
     here means the model's own indicator windows warm up on that
     period), tested/reported on the final 1 year separately.
  3) A yearly behavior breakdown — realized volatility, average daily
     range, and trend/mean-reversion tendency (via Hurst) per calendar
     year — so we can see how Nifty's character has shifted, as a
     foundation for tuning the system to match.

Uses your EXACT live Nifty settings, fetched fresh from the running
app's /config/indices, same as finalized_settings_test.py.

Usage:
    python scripts/nifty_mastery_test.py
"""
import sys
import csv
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import requests

from config.settings import settings
from engine.data_layer.fyers_client.auth import get_fyers_model
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.signal_layer.volatility.realized_vol import compute_realized_vol
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

sys.path.insert(0, str(Path(__file__).parent))
from premium_model import black_scholes_premium, days_to_next_weekly_expiry

TOKEN_PATH = Path(".fyers_token")
CACHE_DIR = Path("nifty_mastery_cache")
CACHE_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_PREFIX = f"nifty_mastery_{RUN_ID}_"

SYMBOL = "NSE:NIFTY50-INDEX"
INDEX_ID = "NIFTY50"
API_BASE = "http://127.0.0.1:8001"

HURST_WINDOW = 100
ZSCORE_WINDOW = 20
DAYS_BACK = 1095  # ~3 years
STRIKE_STEP = 50

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
        log("Make sure uvicorn is running before running this test.")
        sys.exit(1)


def read_token() -> str:
    if not TOKEN_PATH.exists():
        log("ERROR: No .fyers_token file found.")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def fetch_three_years_candles() -> list[dict]:
    token = read_token()
    fyers = get_fyers_model(token)

    end_date = datetime.now()
    all_candles = []
    chunk_days = 90
    remaining_days = DAYS_BACK
    chunk_end = end_date

    log(f"Fetching {DAYS_BACK} days (~3 years) of 1-min candles for {INDEX_ID}...")
    log("This chains many 90-day requests — will take a few minutes.")
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
                f"{' (EMPTY — likely no data this far back)' if len(chunk_candles) == 0 else ''}")
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
        actual_days = (last_date - first_date).days
        if actual_days < DAYS_BACK - 30:
            log(f"NOTE: requested ~{DAYS_BACK} days but only received ~{actual_days} days of real data — "
                f"this is Fyers' actual available history, not a bug in this script.")
    return all_candles


def get_candles(force_refresh: bool) -> list[dict]:
    cache_file = CACHE_DIR / "nifty_3yr_candles.json"
    if cache_file.exists() and not force_refresh:
        log(f"Using cached candles from {cache_file}")
        return json.loads(cache_file.read_text())
    candles = fetch_three_years_candles()
    cache_file.write_text(json.dumps(candles))
    return candles


def precompute_signals(closes: np.ndarray):
    n = len(closes)
    directions = [None] * n
    confidences = np.zeros(n)
    hurst_values = [None] * n
    for i in range(HURST_WINDOW, n):
        window = closes[max(0, i - HURST_WINDOW):i]
        hurst_result = compute_hurst_exponent(window.tolist())
        zscore_result = compute_zscore(window[-ZSCORE_WINDOW:].tolist(), window=ZSCORE_WINDOW)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)
        directions[i] = quant_result.get("direction")
        confidences[i] = quant_result.get("confidence", 0.0)
        hurst_values[i] = hurst_result.get("hurst")
    return directions, confidences, hurst_values


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
    dt = datetime.fromtimestamp(epoch, tz=IST)
    return dt.strftime("%d %b %y"), dt.strftime("%I:%M %p")


def find_same_day_window_start(candles: list[dict], i: int, candle_dates: list, max_window: int) -> int:
    """
    Returns the earliest index within [i - max_window, i] that stays on
    the SAME calendar day as candle i. FIXES A REAL, CONFIRMED BUG: the
    volatility window previously crossed overnight gaps, letting a
    single yesterday-close -> today-open price jump get treated as one
    giant intraday return, inflating "realized volatility" dramatically
    (verified: an 85.72% reading from a single overnight gap, vs 5.15%
    for genuine intraday noise on otherwise-identical data). This fed
    directly into the premium model, causing severe overpricing at every
    market-open entry -- confirmed against a real trade (strike 23250,
    modeled Rs 383.15 vs the real market's Rs 221, a 73% overestimate).
    """
    current_date = candle_dates[i]
    start = i
    for j in range(i, max(0, i - max_window) - 1, -1):
        if candle_dates[j] != current_date:
            break
        start = j
    return start


def run_trade_simulation(candles: list[dict], directions: list, confidences: np.ndarray,
                          confirmed: list, risk_params: dict) -> list[dict]:
    """
    Real Black-Scholes-based premium simulation, replacing the previous
    hardcoded-constant-Rs-60 model — a confirmed, serious flaw (every
    one of 2,523 trades in an earlier 3-year run showed an identical
    entry premium, regardless of actual market conditions at entry).

    Premium is now computed fresh at entry AND at every subsequent
    check, using: real spot price at that candle, the selected strike,
    real days-to-weekly-expiry, and realized volatility computed from
    the actual trailing price window at that point in time (as an IV
    proxy, since real historical option-chain IV doesn't exist for
    backtesting — a separate, already-documented limitation, distinct
    from the constant-premium bug being fixed here).
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

    capital = 50000.0
    vol_window = 100  # candles used to estimate realized vol at each point, matching Group A's default
    candle_dates = [datetime.fromtimestamp(c["epoch"], tz=IST).date() if c.get("epoch") else None for c in candles]

    trades = []
    open_trade = None

    for i in range(n):
        current_price = closes[i]
        current_epoch = candles[i].get("epoch", 0)
        current_dt = datetime.fromtimestamp(current_epoch, tz=IST) if current_epoch else datetime.now(tz=IST)

        # Realized vol as an IV proxy, computed fresh at every step from
        # the real trailing window -- this is what makes premiums
        # actually respond to changing market conditions instead of
        # being frozen, which was the root cause of the bug being fixed.
        same_day_start = find_same_day_window_start(candles, i, candle_dates, vol_window)
        vol_window_closes = closes[same_day_start:i + 1].tolist()
        rv_result = compute_realized_vol(vol_window_closes) if len(vol_window_closes) >= 20 else {}
        current_vol_pct = rv_result.get("realized_vol_pct") or 12.0  # sane fallback if window too short

        if open_trade is not None:
            # days_to_next_weekly_expiry already reflects the REAL time
            # remaining as of THIS candle's actual date — no further
            # adjustment needed. Subtracting elapsed-since-entry on top
            # of this would double-count decay (the earlier version of
            # this code did exactly that, and it drove premiums toward
            # near-zero within minutes of entry — an unrealistic decay
            # rate, since weekly options don't lose ~100% of value in a
            # few minutes under normal conditions).
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
        if sizing["lots"] > 500:
            # Defense-in-depth: 500 lots of Nifty (12,500 shares) is far
            # beyond anything a real Rs 50k-150k account could size,
            # even at aggressive risk settings. If this ever triggers,
            # it means an upstream premium/capital value is corrupted
            # (this is exactly the failure mode that produced the
            # earlier 90-digit lot counts) — skip rather than open a
            # nonsensical position, so a single bad candle can't corrupt
            # the rest of the simulation.
            continue

        open_trade = {
            "entry_index": i, "entry_epoch": candles[i].get("epoch", 0), "entry_index_price": current_price,
            "entry_premium": entry_premium, "direction": direction, "lots": sizing["lots"],
            "peak_profit_pct": 0.0, "strike": strike,
        }

    return trades


def write_trade_csv(trades: list[dict], label: str):
    if not trades:
        log(f"  No trades for {label}, skipping CSV.")
        return
    path = f"{OUTPUT_PREFIX}{label}_trade_log.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_HEADERS)
        writer.writeheader()
        writer.writerows(trades)
    log(f"  Wrote {path} ({len(trades)} trades)")


def summarize(trades: list[dict], label: str):
    if not trades:
        print(f"\n{label}: no trades.")
        return
    wins = [t for t in trades if t["P&L"] > 0]
    total_pnl = sum(t["P&L"] for t in trades)
    print(f"\n{label}: {len(trades)} trades, win rate {len(wins)/len(trades)*100:.1f}%, "
          f"total P&L {total_pnl:,.2f}, return on 50k capital: {total_pnl/50000*100:.1f}%")


# ---------------- Yearly behavior analysis ----------------

def analyze_yearly_behavior(candles: list[dict], hurst_values: list):
    """
    Breaks the full candle history down by calendar year and reports:
    realized volatility (annualized-ish, using daily closes), average
    daily range, and the dominant Hurst regime (trending / mean-
    reverting / random-walk) for that year — a factual description of
    how Nifty actually behaved each year, as a basis for discussing
    whether/how to tune the system per-regime later.
    """
    by_year = {}
    for i, c in enumerate(candles):
        dt = datetime.fromtimestamp(c.get("epoch", 0), tz=IST)
        year = dt.year
        by_year.setdefault(year, {"closes": [], "highs": [], "lows": [], "hurst_values": []})
        by_year[year]["closes"].append(c["close"])
        by_year[year]["highs"].append(c["high"])
        by_year[year]["lows"].append(c["low"])
        if i < len(hurst_values) and hurst_values[i] is not None:
            by_year[year]["hurst_values"].append(hurst_values[i])

    print("\n" + "=" * 70)
    print("NIFTY YEARLY BEHAVIOR ANALYSIS")
    print("=" * 70)

    yearly_summary = {}
    for year in sorted(by_year.keys()):
        data = by_year[year]
        closes = data["closes"]
        if len(closes) < 50:
            continue

        rv_result = compute_realized_vol(closes)
        realized_vol_pct = rv_result.get("realized_vol_pct")

        avg_range_pct = np.mean([(h - l) / l * 100 for h, l in zip(data["highs"], data["lows"]) if l > 0])

        hurst_vals = data["hurst_values"]
        avg_hurst = np.mean(hurst_vals) if hurst_vals else None
        if avg_hurst is not None:
            if avg_hurst > 0.55:
                regime_label = "trending"
            elif avg_hurst < 0.45:
                regime_label = "mean_reverting"
            else:
                regime_label = "random_walk"
        else:
            regime_label = "unknown"

        year_return_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else None

        print(f"\n{year}:")
        print(f"  Candles: {len(closes)}")
        print(f"  Price range: {min(closes):.1f} - {max(closes):.1f}")
        print(f"  Year price return: {year_return_pct:.1f}%" if year_return_pct is not None else "  Year price return: n/a")
        print(f"  Realized volatility: {realized_vol_pct}%" if realized_vol_pct is not None else "  Realized volatility: n/a")
        print(f"  Avg intraday-minute range: {avg_range_pct:.3f}%")
        print(f"  Avg Hurst exponent: {avg_hurst:.3f}" if avg_hurst is not None else "  Avg Hurst: n/a")
        print(f"  Dominant regime: {regime_label}")

        yearly_summary[year] = {
            "candle_count": len(closes), "price_min": min(closes), "price_max": max(closes),
            "year_return_pct": round(year_return_pct, 2) if year_return_pct is not None else None,
            "realized_vol_pct": realized_vol_pct, "avg_intraday_range_pct": round(avg_range_pct, 3),
            "avg_hurst": round(avg_hurst, 3) if avg_hurst is not None else None, "dominant_regime": regime_label,
        }

    summary_path = f"{OUTPUT_PREFIX}yearly_behavior_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(yearly_summary, f, indent=2)
    log(f"\nYearly behavior summary written to {summary_path}")


def main():
    log("=" * 70)
    log("NIFTY MASTERY TEST — 3-year full + walk-forward + yearly behavior")
    log("=" * 70)

    nifty_config = fetch_live_nifty_config()
    log(f"\nLive Nifty settings: mode={nifty_config['mode']}, {nifty_config['risk_params']}")
    if nifty_config["mode"] == "off":
        log("WARNING: Nifty is currently set to Off. Proceeding anyway using its stored risk params.")

    refresh = input("\nForce fresh Fyers data fetch even if cache exists? (y/n): ").strip().lower() == "y"
    confirm = input("Proceed with ~3 years of data fetch + full analysis? This will take several minutes. (y/n): ").strip().lower()
    if confirm != "y":
        log("Cancelled.")
        return

    t_start = time.time()
    candles = get_candles(force_refresh=refresh)

    if len(candles) < HURST_WINDOW + 100:
        log(f"ERROR: only {len(candles)} candles available, need substantially more for a meaningful test.")
        sys.exit(1)

    closes = np.array([c["close"] for c in candles])

    log("\nPrecomputing signals across the full dataset (one pass, reused for all analyses)...")
    t0 = time.time()
    directions, confidences, hurst_values = precompute_signals(closes)
    confirmed = precompute_chart_confirmations(candles, directions)
    log(f"Signal precomputation done in {time.time()-t0:.1f}s")

    risk_params = nifty_config["risk_params"]

    # 1) FULL trade log (all ~3 years)
    log("\n--- Running FULL 3-year trade simulation ---")
    full_trades = run_trade_simulation(candles, directions, confidences, confirmed, risk_params)
    write_trade_csv(full_trades, "FULL_3YR")
    summarize(full_trades, "FULL 3-YEAR")

    # 2) WALK-FORWARD: first ~2 years vs final ~1 year
    total_days_span = (datetime.fromtimestamp(candles[-1]["epoch"], tz=IST) - datetime.fromtimestamp(candles[0]["epoch"], tz=IST)).days
    split_days = total_days_span - 365  # leave the final ~1 year as test
    split_epoch = candles[0]["epoch"] + split_days * 86400
    split_idx = next((i for i, c in enumerate(candles) if c["epoch"] >= split_epoch), len(candles) // 3 * 2)

    train_candles = candles[:split_idx]
    test_candles = candles[split_idx:]
    log(f"\n--- Walk-forward split: train={len(train_candles)} candles, test={len(test_candles)} candles ---")

    test_directions = directions[split_idx:]
    test_confidences = confidences[split_idx:]
    test_confirmed = confirmed[split_idx:]

    log("Running WALK-FORWARD TEST (final ~1 year) trade simulation...")
    wf_test_trades = run_trade_simulation(test_candles, test_directions, test_confidences, test_confirmed, risk_params)
    write_trade_csv(wf_test_trades, "WALKFORWARD_TEST_1YR")
    summarize(wf_test_trades, "WALK-FORWARD TEST (final 1 year)")

    train_directions = directions[:split_idx]
    train_confidences = confidences[:split_idx]
    train_confirmed = confirmed[:split_idx]
    log("Running WALK-FORWARD TRAIN (first ~2 years) trade simulation (reference only)...")
    wf_train_trades = run_trade_simulation(train_candles, train_directions, train_confidences, train_confirmed, risk_params)
    write_trade_csv(wf_train_trades, "WALKFORWARD_TRAIN_2YR")
    summarize(wf_train_trades, "WALK-FORWARD TRAIN (first 2 years, reference only)")

    # 3) Yearly behavior analysis
    analyze_yearly_behavior(candles, hurst_values)

    total_time = (time.time() - t_start) / 60
    log(f"\n\nTOTAL RUNTIME: {total_time:.1f} minutes")
    log(f"All output files prefixed with: {OUTPUT_PREFIX}")


if __name__ == "__main__":
    main()
