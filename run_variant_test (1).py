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


def run_variant(candles: list[dict], vwap_series: list[float | None], use_chart: bool, use_vwap: bool) -> dict:
    """
    Same core loop as engine/backtest/engine.py's run_backtest, with
    the chart-structure and VWAP legs made optional per variant. Quant
    leg (Hurst + Z-score) always runs -- it's the one leg present in
    every variant per the test design.
    """
    lot_size = LOT_SIZES.get(INDEX_ID.upper())
    trades = []
    open_trade = None
    capital = INITIAL_CAPITAL

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
                premium_drop_pct=HARD_SL_PCT, time_stop_minutes=999999,
                has_moved_favorably=open_trade["peak_profit_pct"] > 0,
            )
            time_triggered = elapsed_minutes >= TIME_STOP_MINUTES and open_trade["peak_profit_pct"] <= 0

            tsl_check = update_trailing_stop(
                peak_profit_pct=open_trade["peak_profit_pct"], current_profit_pct=profit_pct,
                activation_pct=TSL_ACTIVATION_PCT, base_trail_pct=TSL_BASE_TRAIL_PCT,
                k=TSL_K, floor_pct=TSL_FLOOR_PCT,
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
            quant_result, chart_result, min_confidence_threshold=MIN_CONFIDENCE,
            vwap_result=vwap_result, require_vwap=use_vwap,
        )

        if not gate_result["fired"]:
            continue

        sizing = compute_position_size(
            available_capital=capital, index_id=INDEX_ID, premium_per_share=APPROX_ENTRY_PREMIUM,
            hard_sl_pct=HARD_SL_PCT, risk_per_trade_pct=RISK_PER_TRADE_PCT,
        )
        if sizing["lots"] < 1:
            continue

        open_trade = {
            "entry_time": current_ts, "entry_candle_index": i, "direction": gate_result["direction"],
            "entry_index_price": current_price, "entry_premium": APPROX_ENTRY_PREMIUM,
            "lots": sizing["lots"], "peak_profit_pct": 0.0,
        }

    return _summarize(trades, INITIAL_CAPITAL)


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


def main():
    walk_forward = "--walk-forward" in sys.argv

    log("=" * 78)
    log("4-VARIANT CONFLUENCE TEST — NIFTY50, mid-2025 onward (real cached candles)")
    log("Uses your REAL confluence gate, quant models, and risk engine — unchanged.")
    if walk_forward:
        log("WALK-FORWARD MODE: 70% train / 30% test, chronological split (no shuffling).")
    log("=" * 78)

    candles = load_candles()
    if len(candles) < HURST_WINDOW + 20:
        log(f"ERROR: insufficient candles ({len(candles)}), need at least {HURST_WINDOW + 20}.")
        sys.exit(1)

    log("Precomputing VWAP series (used by variants 2 & 4 only)...")
    vwap_series = compute_vwap_series(candles)

    if not walk_forward:
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
