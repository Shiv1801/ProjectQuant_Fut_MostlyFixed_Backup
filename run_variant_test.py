"""
4-Variant Confluence Test — standalone, does NOT modify any existing
engine/api files or live code/config. Runs your REAL confluence gate
(engine/confluence_gate/gate.py, evaluate_confluence) and REAL quant/
chart functions unchanged, against the existing cached candle history
(vwap_5yr_cache/nifty_5yr_candles.json), toggling which legs feed into
the gate for each variant:

  1. Quant only          -> chart leg skipped, VWAP leg skipped
  2. Quant + VWAP         -> chart leg skipped, VWAP leg required
  3. Quant + 1-min chart  -> chart leg required, VWAP leg skipped
  4. Current system (all) -> chart leg required, VWAP leg required

Same premium/exit logic as engine/backtest/engine.py (constant-delta
approximation, hard SL / time stop / TSL) so all 4 variants are
directly comparable to each other and to your live backtest engine's
existing methodology. No Fyers API calls -- runs entirely on the
already-cached candle file, so it works regardless of market hours or
the currently-unresolved History-endpoint issue.

Usage:
    python run_variant_test.py
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")
VWAP_START_DATE = datetime(2025, 6, 1, tzinfo=IST)  # matches gate.py's documented mid-2025 usable-volume finding
INDEX_ID = "NIFTY50"
HURST_WINDOW = 100
ZSCORE_WINDOW = 20

# Same defaults as engine/backtest/engine.py's run_backtest signature
# -- overridden below by REAL_SETTINGS when --real-settings is passed.
INITIAL_CAPITAL = 100000.0
RISK_PER_TRADE_PCT = 2.0
HARD_SL_PCT = 27.0
TIME_STOP_MINUTES = 18
TSL_ACTIVATION_PCT = 15.0
TSL_BASE_TRAIL_PCT = 12.0
TSL_K = 0.15
TSL_FLOOR_PCT = 5.0
MIN_CONFIDENCE = 0.5
APPROX_ENTRY_PREMIUM = 60.0

# Real, currently-configured LIVE risk settings for NIFTY50 (mode: both),
# as given directly by Shivam from the app's Risk Settings (Live) panel.
# Used only when --real-settings is passed on the command line.
REAL_SETTINGS = {
    "initial_capital": 30000.0,
    "risk_per_trade_pct": 1.0,
    "hard_sl_pct": 3.5,
    "time_stop_minutes": 3,
    "tsl_activation_pct": 5.0,
    "tsl_base_trail_pct": 3.0,
    "tsl_k": 0.15,
    "tsl_floor_pct": 5.0,
    "require_vwap": True,  # this index's real live config always requires VWAP
}
MIN_CONFIDENCE_SWEEP = [0.85, 0.90, 0.95, 1.00]

APPROX_DELTA = 0.35

VARIANTS = {
    "1. Quant only":            {"use_chart": False, "use_vwap": False},
    "2. Quant + VWAP":          {"use_chart": False, "use_vwap": True},
    "3. Quant + 1-min chart":   {"use_chart": True,  "use_vwap": False},
    "4. Current system (all)":  {"use_chart": True,  "use_vwap": True},
}


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        data = json.load(f)
    # Restrict to mid-2025 onward, matching the only window with usable
    # real volume -- same restriction your own earlier VWAP scripts
    # applied, for the same documented reason.
    cutoff = VWAP_START_DATE.timestamp()
    filtered = [c for c in data if c["epoch"] >= cutoff]
    log(f"Loaded {len(data)} total cached candles, filtered to {len(filtered)} from {VWAP_START_DATE.strftime('%Y-%m-%d')} onward.")
    return filtered


def compute_vwap_series(candles: list[dict]) -> list[float | None]:
    """
    Identical logic to scripts/test_vwap.py and scripts/parameter_sweep_vwap.py's
    compute_vwap_series -- standard VWAP, resets each real IST trading
    day, typical price (H+L+C)/3 weighted by volume.
    """
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


def _simulate_option_premium(entry_index_price: float, current_index_price: float,
                              direction: str, entry_premium: float, delta: float = 0.35) -> float:
    """Identical to engine/backtest/engine.py's _simulate_option_premium."""
    index_move = current_index_price - entry_index_price
    if direction == "long_put":
        index_move = -index_move
    premium_change = index_move * delta
    return max(0.01, entry_premium + premium_change)


def run_variant(candles: list[dict], vwap_series: list[float | None], use_chart: bool, use_vwap: bool,
                 initial_capital: float = None, risk_per_trade_pct: float = None, hard_sl_pct: float = None,
                 time_stop_minutes: int = None, tsl_activation_pct: float = None, tsl_base_trail_pct: float = None,
                 tsl_k: float = None, tsl_floor_pct: float = None, min_confidence: float = None) -> dict:
    """
    Same core loop as engine/backtest/engine.py's run_backtest, with
    the chart-structure and VWAP legs made optional per variant. Quant
    leg (Hurst + Z-score) always runs -- it's the one leg present in
    every variant per the test design.

    All risk/sizing parameters are optional; any left as None falls
    back to the module-level defaults (INITIAL_CAPITAL, HARD_SL_PCT,
    etc.), preserving the exact behavior of every earlier test mode
    (--rolling, --final, --walk-forward, plain). Passing them
    explicitly (as --real-settings does) overrides those defaults with
    the real, currently-configured live risk settings instead.
    """
    _initial_capital = initial_capital if initial_capital is not None else INITIAL_CAPITAL
    _risk_per_trade_pct = risk_per_trade_pct if risk_per_trade_pct is not None else RISK_PER_TRADE_PCT
    _hard_sl_pct = hard_sl_pct if hard_sl_pct is not None else HARD_SL_PCT
    _time_stop_minutes = time_stop_minutes if time_stop_minutes is not None else TIME_STOP_MINUTES
    _tsl_activation_pct = tsl_activation_pct if tsl_activation_pct is not None else TSL_ACTIVATION_PCT
    _tsl_base_trail_pct = tsl_base_trail_pct if tsl_base_trail_pct is not None else TSL_BASE_TRAIL_PCT
    _tsl_k = tsl_k if tsl_k is not None else TSL_K
    _tsl_floor_pct = tsl_floor_pct if tsl_floor_pct is not None else TSL_FLOOR_PCT
    _min_confidence = min_confidence if min_confidence is not None else MIN_CONFIDENCE

    lot_size = LOT_SIZES.get(INDEX_ID.upper())
    trades = []
    open_trade = None
    capital = _initial_capital

    for i in range(HURST_WINDOW, len(candles)):
        window = candles[max(0, i - HURST_WINDOW):i]
        closes = [c["close"] for c in window if c["close"] is not None]

        current_candle = candles[i]
        current_price = current_candle["close"]
        current_ts = datetime.fromtimestamp(current_candle["epoch"], tz=IST).strftime("%Y-%m-%d %H:%M:%S")

        if open_trade is not None:
            sim_premium = _simulate_option_premium(
                open_trade["entry_index_price"], current_price, open_trade["direction"],
                open_trade["entry_premium"], APPROX_DELTA,
            )
            profit_pct = (sim_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            entry_idx = open_trade["entry_candle_index"]
            elapsed_minutes = i - entry_idx

            hard_check = check_hard_stop_loss(
                entry_price=open_trade["entry_premium"], current_price=sim_premium,
                entry_ts=datetime.min, now=datetime.min,
                premium_drop_pct=_hard_sl_pct, time_stop_minutes=999999,
                has_moved_favorably=open_trade["peak_profit_pct"] > 0,
            )
            time_triggered = elapsed_minutes >= _time_stop_minutes and open_trade["peak_profit_pct"] <= 0

            tsl_check = update_trailing_stop(
                peak_profit_pct=open_trade["peak_profit_pct"], current_profit_pct=profit_pct,
                activation_pct=_tsl_activation_pct, base_trail_pct=_tsl_base_trail_pct,
                k=_tsl_k, floor_pct=_tsl_floor_pct,
            )

            should_close = hard_check["triggered"] or time_triggered or tsl_check["triggered"]
            if should_close:
                reason = "hard_sl_hit" if hard_check["triggered"] else ("time_stop" if time_triggered else "tsl_hit")
                pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                capital += pnl
                trades.append({
                    "entry_time": open_trade["entry_time"], "exit_time": current_ts,
                    "direction": open_trade["direction"], "exit_reason": reason,
                    "pnl": round(pnl, 2), "pnl_pct": round(profit_pct, 2),
                    "hold_minutes": elapsed_minutes,
                })
                open_trade = None
            continue

        hurst_result = compute_hurst_exponent(closes)
        zscore_result = compute_zscore(closes[-ZSCORE_WINDOW:] if len(closes) >= ZSCORE_WINDOW else closes, window=ZSCORE_WINDOW)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)

        if not quant_result.get("direction"):
            continue

        if use_chart:
            chart_candles = window + [current_candle]
            chart_result = confirm_chart_structure(chart_candles, quant_result["direction"])
        else:
            # Chart leg skipped for this variant -- pass a pre-confirmed
            # pass-through so evaluate_confluence's Leg 2 check doesn't
            # veto (fired stays gated on Leg 1/quant only, as intended).
            chart_result = {"verdict": "aligned", "confirmed": True}

        vwap_result = None
        if use_vwap:
            vwap_at_i = vwap_series[i]
            if vwap_at_i is not None:
                vwap_result = {"price": current_price, "vwap": vwap_at_i}

        gate_result = evaluate_confluence(
            quant_result, chart_result, min_confidence_threshold=_min_confidence,
            vwap_result=vwap_result, require_vwap=use_vwap,
        )

        if not gate_result["fired"]:
            continue

        sizing = compute_position_size(
            available_capital=capital, index_id=INDEX_ID, premium_per_share=APPROX_ENTRY_PREMIUM,
            hard_sl_pct=_hard_sl_pct, risk_per_trade_pct=_risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue

        open_trade = {
            "entry_time": current_ts, "entry_candle_index": i, "direction": gate_result["direction"],
            "entry_index_price": current_price, "entry_premium": APPROX_ENTRY_PREMIUM,
            "lots": sizing["lots"], "peak_profit_pct": 0.0,
        }

    return _summarize(trades, _initial_capital)


def _summarize(trades: list[dict], initial_capital: float) -> dict:
    if not trades:
        return {"trade_count": 0, "win_rate": None, "total_pnl": 0.0, "return_pct": 0.0, "max_drawdown_pct": 0.0}

    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)

    running_capital = initial_capital
    peak_capital = initial_capital
    max_dd_pct = 0.0
    for t in trades:
        running_capital += t["pnl"]
        peak_capital = max(peak_capital, running_capital)
        dd_pct = (peak_capital - running_capital) / peak_capital * 100 if peak_capital > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round(total_pnl / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
    }


def run_rolling_walk_forward(candles: list[dict], vwap_series: list[float | None], n_windows: int = 5):
    """
    Splits the candle history into n_windows sequential, non-overlapping
    windows. For each window, trains on the portion before it and tests
    on the window itself -- i.e. window k's "train" is everything from
    the start up to window k, and its "test" is window k. This walks
    forward through time (unlike a single 70/30 split), so we can see
    whether each variant's edge is consistent across different periods
    or only appears in one particular slice.
    """
    n = len(candles)
    window_size = n // (n_windows + 2)  # reserve the first 2 window-sizes purely as initial training data
    initial_train_end = window_size * 2

    windows = []
    for k in range(n_windows):
        test_start = initial_train_end + k * window_size
        test_end = min(test_start + window_size, n)
        if test_start >= n:
            break
        windows.append((0, test_start, test_start, test_end))  # (train_start, train_end, test_start, test_end)

    all_results = {label: [] for label in VARIANTS}

    for idx, (tr_s, tr_e, te_s, te_e) in enumerate(windows, 1):
        train_candles = candles[tr_s:tr_e]
        test_candles = candles[te_s:te_e]
        train_vwap = vwap_series[tr_s:tr_e]
        test_vwap = vwap_series[te_s:te_e]

        test_start_date = datetime.fromtimestamp(test_candles[0]["epoch"], tz=IST).strftime("%Y-%m-%d")
        test_end_date = datetime.fromtimestamp(test_candles[-1]["epoch"], tz=IST).strftime("%Y-%m-%d")
        log(f"\nWindow {idx}/{len(windows)}: test period {test_start_date} to {test_end_date} ({len(test_candles)} candles, train on {len(train_candles)} prior candles)")

        for label, cfg in VARIANTS.items():
            test_result = run_variant(test_candles, test_vwap, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"])
            test_result["window"] = idx
            test_result["period"] = f"{test_start_date} to {test_end_date}"
            all_results[label].append(test_result)
            log(f"  {label}: {test_result['trade_count']} trades, {test_result['win_rate']}% WR, {test_result['return_pct']}% return")

    return all_results


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


def run_final_report(candles: list[dict], vwap_series: list[float | None], n_windows: int = 12, starting_capital: float = 50000.0):
    """
    Comprehensive final evaluation, designed to be the last test needed:
      - More, smaller windows (default 12, vs. the earlier 5) for a
        firmer statistical read -- fewer trades per window individually,
        but far more independent samples of "did this variant work in
        this period."
      - Both COMPOUNDED capital (realistic: each window's P&L rolls into
        the next) AND FIXED-capital returns (each window starts fresh at
        starting_capital) are reported -- compounded shows what actually
        happens to real money; fixed-capital isolates each variant's
        per-period edge from the order in which good/bad windows happened
        to fall, since compounding order can flatter or punish a variant
        for reasons unrelated to its real edge.
      - Consistency metrics: win-rate of windows (how many were
        profitable), average return, standard deviation of returns
        (higher = more erratic), and a simple return/stdev ratio as a
        rough risk-adjusted score (higher = more return per unit of
        volatility, i.e. more reliable).
      - Worst single window (max single-period loss) reported explicitly
        -- the realistic worst case an actual trader using this variant
        would have lived through at some point in this history.
      - Total trade count across all windows reported so the sample size
        behind every number is visible, not hidden.
    """
    n = len(candles)
    window_size = n // (n_windows + 2)
    initial_train_end = window_size * 2

    windows = []
    for k in range(n_windows):
        test_start = initial_train_end + k * window_size
        test_end = min(test_start + window_size, n)
        if test_start >= n:
            break
        windows.append((test_start, test_end))

    log(f"\nRunning FINAL REPORT: {len(windows)} independent windows (~{window_size} candles each)...")

    per_variant_window_results = {label: [] for label in VARIANTS}

    for idx, (te_s, te_e) in enumerate(windows, 1):
        test_candles = candles[te_s:te_e]
        test_vwap = vwap_series[te_s:te_e]
        test_start_date = datetime.fromtimestamp(test_candles[0]["epoch"], tz=IST).strftime("%Y-%m-%d")
        test_end_date = datetime.fromtimestamp(test_candles[-1]["epoch"], tz=IST).strftime("%Y-%m-%d")
        log(f"  Window {idx}/{len(windows)}: {test_start_date} to {test_end_date}")

        for label, cfg in VARIANTS.items():
            result = run_variant(test_candles, test_vwap, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"])
            result["window"] = idx
            result["period"] = f"{test_start_date} to {test_end_date}"
            per_variant_window_results[label].append(result)

    # --- Compute all summary statistics per variant ---
    summary = {}
    for label, window_results in per_variant_window_results.items():
        returns = [r["return_pct"] for r in window_results]
        trade_counts = [r["trade_count"] for r in window_results]
        total_trades = sum(trade_counts)

        profitable_windows = sum(1 for r in returns if r > 0)
        avg_return = sum(returns) / len(returns) if returns else 0
        return_stdev = _stdev(returns)
        risk_adjusted_score = (avg_return / return_stdev) if return_stdev > 0 else 0
        worst_window_idx = min(range(len(returns)), key=lambda i: returns[i]) if returns else None
        worst_window_return = returns[worst_window_idx] if worst_window_idx is not None else 0
        worst_window_period = window_results[worst_window_idx]["period"] if worst_window_idx is not None else ""

        # Compounded capital: apply each window's return sequentially to a running balance
        compounded_capital = starting_capital
        for r in returns:
            compounded_capital *= (1 + r / 100)
        compounded_total_return_pct = (compounded_capital - starting_capital) / starting_capital * 100

        # Fixed-capital: each window's return applied independently to the SAME starting capital,
        # then summed -- shows aggregate edge independent of compounding order.
        fixed_capital_sum_pct = sum(returns)

        summary[label] = {
            "windows": len(returns),
            "total_trades": total_trades,
            "profitable_windows": profitable_windows,
            "profitable_pct": round(profitable_windows / len(returns) * 100, 1) if returns else 0,
            "avg_return_per_window": round(avg_return, 2),
            "return_stdev": round(return_stdev, 2),
            "risk_adjusted_score": round(risk_adjusted_score, 3),
            "worst_window_return": round(worst_window_return, 2),
            "worst_window_period": worst_window_period,
            "compounded_final_capital": round(compounded_capital, 2),
            "compounded_total_return_pct": round(compounded_total_return_pct, 2),
            "fixed_capital_sum_return_pct": round(fixed_capital_sum_pct, 2),
        }

    return per_variant_window_results, summary


def main():
    walk_forward = "--walk-forward" in sys.argv
    rolling = "--rolling" in sys.argv
    final = "--final" in sys.argv
    real_settings = "--real-settings" in sys.argv

    log("=" * 78)
    log("4-VARIANT CONFLUENCE TEST — NIFTY50, mid-2025 onward (real cached candles)")
    log("Uses your REAL confluence gate, quant models, and risk engine — unchanged.")
    if walk_forward:
        log("WALK-FORWARD MODE: 70% train / 30% test, chronological split (no shuffling).")
    if rolling:
        log("ROLLING WALK-FORWARD MODE: multiple sequential test windows.")
    if final:
        log("FINAL REPORT MODE: 12 independent windows, compounded + fixed-capital + risk-adjusted stats.")
    if real_settings:
        log("REAL SETTINGS MODE: using your actual configured live risk parameters (Hard SL 3.5%, "
            "Time Stop 3min, Risk/Trade 1%, Capital Rs 30,000, require_vwap=True), "
            f"sweeping Min Confidence across {MIN_CONFIDENCE_SWEEP}.")
    log("=" * 78)


    candles = load_candles()
    if len(candles) < HURST_WINDOW + 20:
        log(f"ERROR: insufficient candles ({len(candles)}), need at least {HURST_WINDOW + 20}.")
        sys.exit(1)

    log("Precomputing VWAP series (used by variants 2 & 4 only)...")
    vwap_series = compute_vwap_series(candles)

    if real_settings:
        # This index's real live config has require_vwap=True fixed --
        # there is no real live mode where VWAP is optional. So this
        # mode reports: (a) the REAL configuration exactly as it runs
        # live today (chart + VWAP both required), and (b) what
        # removing each leg would have done at these SAME real risk
        # parameters, for comparison -- not 4 equally-valid live modes.
        print("\n" + "=" * 120)
        print("REAL LIVE SETTINGS TEST — NIFTY50, Hard SL 3.5%, Time Stop 3min, TSL 5%/3%/0.15/5%, "
              "Risk/Trade 1%, Capital Rs 30,000, require_vwap=True")
        print("=" * 120)

        for min_conf in MIN_CONFIDENCE_SWEEP:
            print(f"\n--- Min Confidence: {min_conf} ---")
            print(f"{'Variant':<28} {'Trades':<9} {'Win Rate':<11} {'Total P&L':<14} {'Return %':<11} {'Max DD %'}")
            print("-" * 100)
            for label, cfg in VARIANTS.items():
                result = run_variant(
                    candles, vwap_series, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"],
                    initial_capital=REAL_SETTINGS["initial_capital"],
                    risk_per_trade_pct=REAL_SETTINGS["risk_per_trade_pct"],
                    hard_sl_pct=REAL_SETTINGS["hard_sl_pct"],
                    time_stop_minutes=REAL_SETTINGS["time_stop_minutes"],
                    tsl_activation_pct=REAL_SETTINGS["tsl_activation_pct"],
                    tsl_base_trail_pct=REAL_SETTINGS["tsl_base_trail_pct"],
                    tsl_k=REAL_SETTINGS["tsl_k"],
                    tsl_floor_pct=REAL_SETTINGS["tsl_floor_pct"],
                    min_confidence=min_conf,
                )
                marker = " <-- REAL LIVE CONFIG" if (label.startswith("4.")) else ""
                wr = f"{result['win_rate']}%" if result['win_rate'] is not None else "N/A"
                print(f"{label:<28} {result['trade_count']:<9} {wr:<11} {result['total_pnl']:<14,.2f} {result['return_pct']:<11} {result['max_drawdown_pct']}{marker}")

        print("\nNote: Variant 4 (Current system, all legs) with require_vwap=True is your REAL live")
        print("configuration exactly as it runs today. Variants 1-3 show what removing legs would")
        print("have done at these SAME real risk parameters, for comparison only.")

    elif final:
        per_variant_window_results, summary = run_final_report(candles, vwap_series, n_windows=12, starting_capital=50000.0)

        print("\n" + "=" * 120)
        print("FINAL REPORT — per-window returns (12 independent windows)")
        print("=" * 120)
        for label, window_results in per_variant_window_results.items():
            print(f"\n{label}:")
            print(f"  {'Window':<9} {'Period':<26} {'Trades':<8} {'Win Rate':<10} {'Return %'}")
            for r in window_results:
                wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "N/A"
                print(f"  {r['window']:<9} {r['period']:<26} {r['trade_count']:<8} {wr:<10} {r['return_pct']}")

        print("\n" + "=" * 120)
        print(f"FINAL SUMMARY — starting capital Rs 50,000")
        print("=" * 120)
        print(f"{'Variant':<24} {'Windows':<9} {'Trades':<8} {'Profitable':<12} {'Avg Ret%':<10} {'StdDev':<9} {'Risk-Adj':<10} {'Worst Window':<12} {'Compounded End':<16} {'Compounded %':<13} {'Fixed-Cap Sum %'}")
        print("-" * 160)
        for label, s in summary.items():
            print(f"{label:<24} {s['windows']:<9} {s['total_trades']:<8} "
                  f"{s['profitable_windows']}/{s['windows']} ({s['profitable_pct']}%)".ljust(12) + " " +
                  f"{s['avg_return_per_window']:<10} {s['return_stdev']:<9} {s['risk_adjusted_score']:<10} "
                  f"{s['worst_window_return']:<12} Rs {s['compounded_final_capital']:<13,.0f} {s['compounded_total_return_pct']:<13} {s['fixed_capital_sum_return_pct']}")

        print("\nColumn notes:")
        print("  Profitable      = number of windows (out of total) with a positive return")
        print("  Avg Ret%        = mean return per window")
        print("  StdDev          = standard deviation of per-window returns (higher = more erratic)")
        print("  Risk-Adj        = Avg Ret% / StdDev (higher = more return per unit of volatility)")
        print("  Worst Window    = the single worst window's return (realistic worst case lived through)")
        print("  Compounded End  = final capital if each window's P&L rolled into the next, starting Rs 50,000")
        print("  Compounded %    = total % return via compounding")
        print("  Fixed-Cap Sum % = sum of each window's return computed independently (order-independent edge)")

    elif rolling:
        all_results = run_rolling_walk_forward(candles, vwap_series, n_windows=5)

        print("\n" + "=" * 110)
        print("ROLLING WALK-FORWARD RESULTS — per-window returns, each window tested independently")
        print("=" * 110)
        for label, window_results in all_results.items():
            returns = [r["return_pct"] for r in window_results]
            positive_windows = sum(1 for r in returns if r > 0)
            print(f"\n{label}:")
            print(f"  {'Window':<9} {'Period':<26} {'Trades':<8} {'Win Rate':<10} {'Return %'}")
            for r in window_results:
                wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "N/A"
                print(f"  {r['window']:<9} {r['period']:<26} {r['trade_count']:<8} {wr:<10} {r['return_pct']}")
            avg_return = sum(returns) / len(returns) if returns else 0
            consistency = f"{positive_windows}/{len(returns)} windows profitable"
            verdict = "CONSISTENT edge" if positive_windows >= len(returns) * 0.7 else \
                      "INCONSISTENT (regime-dependent or noise)" if positive_windows >= len(returns) * 0.4 else \
                      "CONSISTENTLY UNPROFITABLE"
            print(f"  --> Avg return per window: {avg_return:.2f}% | {consistency} | {verdict}")

    elif not walk_forward:
        results = {}
        for label, cfg in VARIANTS.items():
            log(f"\nRunning: {label} ...")
            result = run_variant(candles, vwap_series, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"])
            results[label] = result
            log(f"  {label}: {result['trade_count']} trades, "
                f"{result['win_rate']}% win rate, "
                f"P&L {result['total_pnl']:,.2f} ({result['return_pct']}%), "
                f"max DD {result['max_drawdown_pct']}%")

        print("\n" + "=" * 100)
        print("RESULTS SUMMARY")
        print("=" * 100)
        print(f"{'Variant':<28} {'Trades':<9} {'Win Rate':<11} {'Total P&L':<14} {'Return %':<11} {'Max DD %'}")
        print("-" * 100)
        for label, r in results.items():
            wr = f"{r['win_rate']}%" if r['win_rate'] is not None else "N/A"
            print(f"{label:<28} {r['trade_count']:<9} {wr:<11} {r['total_pnl']:<14,.2f} {r['return_pct']:<11} {r['max_drawdown_pct']}")

    else:
        # Chronological 70/30 split -- train is the earlier period, test
        # is the later, genuinely unseen period. This is a real
        # out-of-sample check, not a shuffle-based split (shuffling
        # would leak future information into "training" for a
        # sequential strategy like this one).
        split_idx = int(len(candles) * 0.7)
        train_candles = candles[:split_idx]
        test_candles = candles[split_idx:]
        train_vwap = vwap_series[:split_idx]
        test_vwap = vwap_series[split_idx:]

        train_start = datetime.fromtimestamp(train_candles[0]["epoch"], tz=IST).strftime("%Y-%m-%d")
        train_end = datetime.fromtimestamp(train_candles[-1]["epoch"], tz=IST).strftime("%Y-%m-%d")
        test_start = datetime.fromtimestamp(test_candles[0]["epoch"], tz=IST).strftime("%Y-%m-%d")
        test_end = datetime.fromtimestamp(test_candles[-1]["epoch"], tz=IST).strftime("%Y-%m-%d")
        log(f"Train period: {train_start} to {train_end} ({len(train_candles)} candles)")
        log(f"Test period:  {test_start} to {test_end} ({len(test_candles)} candles)")

        results = {}
        for label, cfg in VARIANTS.items():
            log(f"\nRunning: {label} (train) ...")
            train_result = run_variant(train_candles, train_vwap, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"])
            log(f"Running: {label} (test) ...")
            test_result = run_variant(test_candles, test_vwap, use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"])
            results[label] = {"train": train_result, "test": test_result}
            log(f"  {label} TRAIN: {train_result['trade_count']} trades, {train_result['win_rate']}% WR, {train_result['return_pct']}% return")
            log(f"  {label} TEST:  {test_result['trade_count']} trades, {test_result['win_rate']}% WR, {test_result['return_pct']}% return")

        print("\n" + "=" * 110)
        print("WALK-FORWARD RESULTS — TRAIN vs TEST (out-of-sample)")
        print("=" * 110)
        print(f"{'Variant':<26} {'Seg':<6} {'Trades':<8} {'Win Rate':<10} {'Return %':<11} {'Max DD %':<10} {'Holds up?'}")
        print("-" * 110)
        for label, r in results.items():
            tr, te = r["train"], r["test"]
            holds_up = "—"
            if tr["return_pct"] and te["return_pct"] is not None:
                if tr["return_pct"] > 0 and te["return_pct"] > 0:
                    holds_up = "YES (both positive)"
                elif tr["return_pct"] > 0 and te["return_pct"] <= 0:
                    holds_up = "NO (train-only, likely overfit)"
                elif tr["return_pct"] <= 0 and te["return_pct"] > 0:
                    holds_up = "MIXED (worse in-sample)"
                else:
                    holds_up = "NO (negative both)"
            wr_tr = f"{tr['win_rate']}%" if tr['win_rate'] is not None else "N/A"
            wr_te = f"{te['win_rate']}%" if te['win_rate'] is not None else "N/A"
            print(f"{label:<26} {'TRAIN':<6} {tr['trade_count']:<8} {wr_tr:<10} {tr['return_pct']:<11} {tr['max_drawdown_pct']:<10} {holds_up}")
            print(f"{'':<26} {'TEST':<6} {te['trade_count']:<8} {wr_te:<10} {te['return_pct']:<11} {te['max_drawdown_pct']:<10}")
            print("-" * 110)

    print("\nNote: option premium is a constant-delta approximation, same methodology")
    print("as engine/backtest/engine.py — not full options pricing. OFI/PCR excluded")
    print("from the quant leg (not retained historically), same as the live backtest engine.")


if __name__ == "__main__":
    main()
