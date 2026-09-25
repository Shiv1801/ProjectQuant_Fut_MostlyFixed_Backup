"""
Full Parameter Sweep — Hard SL x Time Stop x TSL Base Trail x Risk/Trade
x Min Confidence, VWAP filter ON, tested against real 2025+2026
Nifty data (restricted to these two years both for realistic runtime,
and because they're the years where VWAP actually has usable real
volume data -- 2021-2024 confirmed to have zero real volume in the
cached data, so a VWAP-based strategy has no meaningful signal to test
there regardless).

Grid (as specified):
  Hard SL %:        0 to 10,   step 0.5   -> 21 values
  Time Stop (min):  1 to 15,   step 1     -> 15 values
  TSL Base Trail %: 3 to 5,    step 0.5   -> 5 values
  Risk/Trade %:     0 to 10,   step 1     -> 11 values
  Min Confidence:   0.6 to 1.0, step 0.05 -> 9 values
  TOTAL: 155,925 combinations

Performance design: the expensive part (confluence gate signal
generation via Hurst/Z-score per candle, chart structure confirmation,
VWAP) does NOT depend on any of the 5 swept parameters -- it's
computed ONCE, then every combination replays those same precomputed
signals through different SL/TSL/time-stop/risk/confidence rules, which
is fast pure arithmetic. This is the same design used in the project's
earlier annual sweep tooling.

Includes the two new exit rules (session-end, strike-scope) in every
combination, matching the live app's current behavior. Includes real
Fyers charges deducted, same as vwap_5yr_detailed_log.py.

Ranks results by a composite score (not raw P&L alone, since we've
directly confirmed in this project that raw P&L can be dominated by a
handful of extreme trades) -- combines win rate, net return, and a
penalty for very low trade counts (a combination with 3 trades and a
100% win rate is not more trustworthy than one with 300 trades and a
55% win rate).

Usage:
    python scripts/parameter_sweep_vwap.py

Requires the 5-year cache already built by vwap_5yr_detailed_log.py.
"""
import sys
import csv
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from itertools import product

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

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
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")
OUTPUT_DIR = Path("sample_test")
INDEX_ID = "NIFTY50"
STRIKE_STEP = 50
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
INITIAL_CAPITAL = 50000.0
STRIKES_TRACKED_PER_SIDE = 10
YEARS_TO_TEST = {2025, 2026}

# --- The grid, exactly as specified ---
HARD_SL_VALUES = [round(0.0 + i * 0.5, 2) for i in range(21)]        # 0.0 .. 10.0
TIME_STOP_VALUES = list(range(1, 16))                                 # 1 .. 15
TSL_BASE_TRAIL_VALUES = [round(3.0 + i * 0.5, 2) for i in range(5)]    # 3.0 .. 5.0
RISK_PER_TRADE_VALUES = list(range(0, 11))                            # 0 .. 10
MIN_CONFIDENCE_VALUES = [round(0.6 + i * 0.05, 2) for i in range(9)]   # 0.6 .. 1.0

TSL_ACTIVATION_PCT = 5.0  # held fixed -- not part of the requested sweep
TSL_K = 0.15
TSL_FLOOR_PCT = 5.0


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_and_filter_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run vwap_5yr_detailed_log.py first.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        all_candles = json.load(f)
    log(f"Loaded {len(all_candles)} total cached candles.")

    filtered = [c for c in all_candles if datetime.fromtimestamp(c["epoch"], tz=IST).year in YEARS_TO_TEST]
    log(f"Filtered to {len(filtered)} candles in years {sorted(YEARS_TO_TEST)}.")
    return filtered


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


def precompute_datetime_and_expiry(candles: list[dict]) -> tuple[list, list, list]:
    """
    Precomputes, for every candle ONCE: the parsed IST datetime, days-
    to-expiry, AND whether session-end would trigger at that candle.
    None of these depend on any swept parameter. Profiling confirmed
    datetime.fromtimestamp() and its downstream calls were a real,
    significant cost when recomputed inside every one of 155,925
    combinations -- this is the same "precompute what doesn't change"
    principle already applied to the gate signals and VWAP.
    """
    n = len(candles)
    dt_series = [None] * n
    days_left_series = [0.0] * n
    session_end_series = [False] * n
    for i, c in enumerate(candles):
        dt = datetime.fromtimestamp(c["epoch"], tz=IST) if c.get("epoch") else datetime.now(tz=IST)
        dt_series[i] = dt
        days_left_series[i] = days_to_next_weekly_expiry(dt)
        session_end_series[i] = check_session_end(dt)["triggered"]
    return dt_series, days_left_series, session_end_series



def precompute_atm_series(closes: np.ndarray) -> list[float]:
    """
    The ATM strike (nearest round strike to spot) for every candle,
    computed ONCE. Profiling identified this round()-based computation
    as the single largest remaining cost when recomputed inside every
    one of 155,925 combinations (64,364 round() calls per combination).
    """
    return [round(price / STRIKE_STEP) * STRIKE_STEP for price in closes]


def precompute_realized_vol_series(candles: list[dict], closes: np.ndarray) -> list[float]:
    """
    Realized vol also doesn't depend on swept params -- compute once.

    CRITICAL FIX: the trailing window is now restricted to SAME-DAY
    candles only. Confirmed via a real, live trade (strike 23250,
    modeled entry premium Rs 383.15 vs the REAL market price of Rs 221
    -- a 73% overestimate) that including the prior day's candles in
    the window let a single overnight price gap (e.g. yesterday's close
    23904 -> today's 9:15 AM open 23250) get treated as one enormous
    minute-over-minute return. Scaled up by the annualization factor
    (sqrt(252*375)), that single gap alone can produce an ~85% "realized
    volatility" reading from what is, in reality, a single overnight
    jump -- verified directly: a synthetic test with an overnight gap
    produced 85.72% vs. 5.15% for genuine intraday noise on the same
    data otherwise. This inflated volatility then fed directly into the
    Black-Scholes premium model, producing wildly overpriced premiums
    at every market-open entry across every backtest run today.

    Same-day-only windowing is also the more faithful, realistic
    choice: a trader's live intuition of "how volatile has it been"
    naturally resets each morning, not carried over an overnight gap
    that isn't real intraday price action.
    """
    n = len(closes)
    vol_series = [12.0] * n  # sane fallback for the earliest candles
    vol_window = 100

    # Precompute each candle's IST date once, so the same-day check is
    # a cheap set-membership lookup, not a fresh datetime parse per candle.
    candle_dates = [datetime.fromtimestamp(c["epoch"], tz=IST).date() if c.get("epoch") else None for c in candles]

    for i in range(n):
        current_date = candle_dates[i]
        window_start = max(0, i - vol_window)
        # Walk backward from window_start until we hit a date change,
        # so the window never crosses a day boundary.
        same_day_start = i
        for j in range(i, window_start - 1, -1):
            if candle_dates[j] != current_date:
                break
            same_day_start = j

        window_closes = closes[same_day_start:i + 1].tolist()
        if len(window_closes) >= 20:
            rv = compute_realized_vol(window_closes)
            vol_series[i] = rv.get("realized_vol_pct") or 12.0
        # else: keep the 12.0 fallback for the first ~20 min of each day,
        # when there isn't yet enough same-day history for a real reading
        # -- this is honest (we genuinely don't know intraday vol yet
        # that early) rather than silently borrowing yesterday's data.
    return vol_series


def run_single_combination(candles: list[dict], closes: np.ndarray, directions: list, confidences: np.ndarray,
                            confirmed: list, vwap_series: list, vol_series: list, dt_series: list,
                            days_left_series: list, session_end_series: list, atm_series: list,
                            hard_sl_pct: float, time_stop_minutes: int, tsl_base_trail_pct: float,
                            risk_per_trade_pct: float, min_confidence: float) -> dict:
    """
    Replays the PRECOMPUTED signals through one specific parameter
    combination. No model recomputation happens here -- this is why the
    full 155,925-combination sweep is tractable at all. dt_series and
    days_left_series are also precomputed (see
    precompute_datetime_and_expiry) since they don't depend on the
    swept parameters either -- profiling showed recomputing these per
    combination was a real, significant cost.
    """
    lot_size = LOT_SIZES.get(INDEX_ID)
    n = len(closes)
    capital = INITIAL_CAPITAL

    trades = []
    open_trade = None

    for i in range(n):
        current_price = closes[i]
        current_dt = dt_series[i]
        current_vol_pct = vol_series[i]

        if open_trade is not None:
            days_left = days_left_series[i]
            option_type = "CE" if open_trade["direction"] == "long_call" else "PE"
            sim_premium = black_scholes_premium(
                spot=current_price, strike=open_trade["strike"], days_to_expiry=days_left,
                volatility_pct=current_vol_pct, option_type=option_type,
            )
            profit_pct = (sim_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            elapsed = i - open_trade["entry_index"]
            drop_pct = (open_trade["entry_premium"] - sim_premium) / open_trade["entry_premium"] * 100

            # FIX: a Hard SL of exactly 0% is a VALID, meaningful
            # setting ("exit on any loss at all") -- it was previously
            # disabled entirely by this guard, which meant 0%-labeled
            # rows in the sweep were silently running with NO stop-loss
            # at all, not a real 0% stop. That produced misleadingly
            # good-looking results for "0% SL" combinations, since they
            # were never actually being tested.
            hard_hit = drop_pct >= hard_sl_pct
            time_hit = elapsed >= time_stop_minutes and open_trade["peak_profit_pct"] <= 0

            tsl_active = open_trade["peak_profit_pct"] >= TSL_ACTIVATION_PCT
            if tsl_active:
                profit_above_activation = open_trade["peak_profit_pct"] - TSL_ACTIVATION_PCT
                trail_distance = max(TSL_FLOOR_PCT, tsl_base_trail_pct - TSL_K * profit_above_activation)
                locked_floor = open_trade["peak_profit_pct"] - trail_distance
                tsl_hit = profit_pct <= locked_floor
            else:
                tsl_hit = False

            session_end_hit = session_end_series[i]
            current_atm = atm_series[i]
            scope_half_width = STRIKES_TRACKED_PER_SIDE * STRIKE_STEP
            strike_out_of_scope = abs(open_trade["strike"] - current_atm) > scope_half_width

            if session_end_hit or strike_out_of_scope or hard_hit or time_hit or tsl_hit:
                reason = ("session_end_exit" if session_end_hit else
                          "strike_out_of_scope" if strike_out_of_scope else
                          "hard_sl_hit" if hard_hit else
                          "time_stop" if time_hit else "tsl_hit")
                gross_pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                charges = compute_round_trip_charges(
                    entry_premium=open_trade["entry_premium"], exit_premium=sim_premium,
                    lots=open_trade["lots"], lot_size=lot_size,
                )
                net_pnl = gross_pnl - charges["total_charges"]
                capital += net_pnl
                trades.append({"reason": reason, "gross_pnl": gross_pnl, "charges": charges["total_charges"], "net_pnl": net_pnl})
                open_trade = None
            continue

        direction = directions[i]
        if direction is None or confidences[i] < min_confidence or not confirmed[i]:
            continue
        if vwap_series[i] is None:
            continue
        vwap_signal = "bullish" if closes[i] > vwap_series[i] else "bearish"
        gate_signal = "bullish" if direction == "long_call" else "bearish"
        if vwap_signal != gate_signal:
            continue

        strike = atm_series[i]
        option_type = "CE" if direction == "long_call" else "PE"
        days_left = days_left_series[i]
        entry_premium = black_scholes_premium(
            spot=current_price, strike=strike, days_to_expiry=days_left,
            volatility_pct=current_vol_pct, option_type=option_type,
        )

        if risk_per_trade_pct <= 0:
            continue  # a 0% risk setting means no position can be sized -- correctly produces zero trades
        sizing = compute_position_size(
            available_capital=capital, index_id=INDEX_ID, premium_per_share=entry_premium,
            hard_sl_pct=max(hard_sl_pct, 0.5), risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1 or sizing["lots"] > 500:
            continue

        open_trade = {
            "entry_index": i, "entry_premium": entry_premium, "direction": direction,
            "lots": sizing["lots"], "peak_profit_pct": 0.0, "strike": strike,
        }

    if not trades:
        return {"trade_count": 0, "win_rate": None, "net_pnl": 0.0, "net_return_pct": 0.0, "composite_score": -999}

    wins = [t for t in trades if t["net_pnl"] > 0]
    net_pnl = sum(t["net_pnl"] for t in trades)
    win_rate = len(wins) / len(trades)
    net_return_pct = net_pnl / INITIAL_CAPITAL * 100

    # Composite score: rewards win rate and return, but penalizes very
    # low trade counts (an n=3 100%-win-rate result is not trustworthy
    # -- directly informed by this project's earlier finding that small
    # samples can be dominated by a handful of extreme trades).
    sample_confidence = min(1.0, len(trades) / 30)  # full confidence at 30+ trades, scaled down below that
    composite_score = (win_rate * 40 + min(net_return_pct, 200) / 200 * 40) * sample_confidence

    return {
        "trade_count": len(trades), "win_rate": round(win_rate * 100, 2),
        "net_pnl": round(net_pnl, 2), "net_return_pct": round(net_return_pct, 2),
        "composite_score": round(composite_score, 3),
    }


def main():
    log("=" * 70)
    log("FULL PARAMETER SWEEP: Hard SL x Time Stop x TSL x Risk x Confidence")
    log("VWAP filter ON, tested on 2025+2026 data")
    log("=" * 70)

    total_combos = len(HARD_SL_VALUES) * len(TIME_STOP_VALUES) * len(TSL_BASE_TRAIL_VALUES) * len(RISK_PER_TRADE_VALUES) * len(MIN_CONFIDENCE_VALUES)
    log(f"\nGrid size: {len(HARD_SL_VALUES)} x {len(TIME_STOP_VALUES)} x {len(TSL_BASE_TRAIL_VALUES)} x "
        f"{len(RISK_PER_TRADE_VALUES)} x {len(MIN_CONFIDENCE_VALUES)} = {total_combos:,} combinations")

    confirm = input(f"\nThis will run {total_combos:,} combinations against 3 years of real data. "
                     f"Expect roughly 25-130+ minutes depending on your machine. Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        log("Cancelled.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    candles = load_and_filter_candles()
    if len(candles) < HURST_WINDOW + 100:
        log(f"ERROR: only {len(candles)} candles in the requested years, need substantially more.")
        sys.exit(1)

    closes = np.array([c["close"] for c in candles])

    log("\nPrecomputing confluence-gate signals (ONE PASS, shared by all 155,925 combinations)...")
    t0 = time.time()
    directions, confidences = precompute_gate_signals(closes)
    confirmed = precompute_chart_confirmations(candles, directions)
    vwap_series = compute_vwap_series(candles)
    vol_series = precompute_realized_vol_series(candles, closes)
    dt_series, days_left_series, session_end_series = precompute_datetime_and_expiry(candles)
    atm_series = precompute_atm_series(closes)
    log(f"Done in {time.time()-t0:.1f}s")

    log(f"\nRunning all {total_combos:,} combinations...")
    t_sweep_start = time.time()
    results = []
    combo_num = 0

    for hard_sl, time_stop, tsl_trail, risk_pct, min_conf in product(
        HARD_SL_VALUES, TIME_STOP_VALUES, TSL_BASE_TRAIL_VALUES, RISK_PER_TRADE_VALUES, MIN_CONFIDENCE_VALUES
    ):
        combo_num += 1
        result = run_single_combination(
            candles, closes, directions, confidences, confirmed, vwap_series, vol_series,
            dt_series, days_left_series, session_end_series, atm_series,
            hard_sl_pct=hard_sl, time_stop_minutes=time_stop, tsl_base_trail_pct=tsl_trail,
            risk_per_trade_pct=risk_pct, min_confidence=min_conf,
        )
        result.update({"hard_sl_pct": hard_sl, "time_stop_minutes": time_stop,
                        "tsl_base_trail_pct": tsl_trail, "risk_per_trade_pct": risk_pct, "min_confidence": min_conf})
        results.append(result)

        if combo_num % 5000 == 0:
            elapsed = time.time() - t_sweep_start
            rate = combo_num / elapsed
            remaining = (total_combos - combo_num) / rate
            log(f"  {combo_num:,}/{total_combos:,} done ({elapsed:.0f}s elapsed, ~{remaining/60:.1f} min remaining)")

    total_sweep_time = time.time() - t_sweep_start
    log(f"\nSweep complete in {total_sweep_time/60:.1f} minutes ({total_combos/total_sweep_time:.0f} combos/sec)")

    # Save the FULL result set (all 155,925 rows) for later analysis
    full_results_path = OUTPUT_DIR / "parameter_sweep_full_results.csv"
    with open(full_results_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["hard_sl_pct", "time_stop_minutes", "tsl_base_trail_pct", "risk_per_trade_pct",
                      "min_confidence", "trade_count", "win_rate", "net_pnl", "net_return_pct", "composite_score"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    log(f"Full results ({len(results):,} rows) written to {full_results_path}")

    # Top 20 by composite score, with a MINIMUM trade count filter so
    # tiny, unreliable samples can't dominate the ranking
    valid_results = [r for r in results if r["trade_count"] >= 10]
    top_20 = sorted(valid_results, key=lambda r: r["composite_score"], reverse=True)[:20]

    print("\n" + "=" * 100)
    print("TOP 20 COMBINATIONS (minimum 10 trades, ranked by composite score)")
    print("=" * 100)
    print(f"{'Rank':<5} {'HardSL%':<9} {'TimeStop':<9} {'TSLTrail%':<10} {'Risk%':<7} {'MinConf':<8} "
          f"{'Trades':<8} {'WinRate%':<9} {'NetP&L':<12} {'NetRet%':<9} {'Score'}")
    print("-" * 100)
    for rank, r in enumerate(top_20, 1):
        print(f"{rank:<5} {r['hard_sl_pct']:<9} {r['time_stop_minutes']:<9} {r['tsl_base_trail_pct']:<10} "
              f"{r['risk_per_trade_pct']:<7} {r['min_confidence']:<8} {r['trade_count']:<8} {r['win_rate']:<9} "
              f"{r['net_pnl']:<12,.2f} {r['net_return_pct']:<9} {r['composite_score']}")

    top_20_path = OUTPUT_DIR / "parameter_sweep_top20.json"
    with open(top_20_path, "w", encoding="utf-8") as f:
        json.dump(top_20, f, indent=2)
    log(f"\nTop 20 written to {top_20_path}")


if __name__ == "__main__":
    main()
