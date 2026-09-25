"""
4-Variant Confluence Test — BLACK-SCHOLES PREMIUM VERSION. Standalone,
does NOT modify any existing engine/api files or live code/config.

Upgrades run_variant_test.py's constant-delta premium approximation to
real Black-Scholes modeling (scripts/premium_model.py) driven by
same-day-only realized volatility (the same windowing fix already
proven in scripts/parameter_sweep_vwap.py, which avoids the overnight-
gap volatility blowup documented there), plus real Fyers round-trip
charges (scripts/fyers_charges.py) deducted from every trade's P&L.

This is a genuine accuracy upgrade over constant-delta, not literal
historical option data -- Fyers does not provide historical premiums
for expired option contracts to anyone (confirmed via Fyers' own
community forum), and no free, bulk/API-accessible alternative exists
either. Black-Scholes with a realized-vol proxy is the same standard
your own existing parameter_sweep_vwap.py already holds itself to.

Tests your REAL configured live risk settings (Hard SL 3.5%, Time Stop
3min, TSL 5%/3%/0.15/5%, Risk/Trade 1%, Capital Rs 30,000,
require_vwap=True for this index), swept across Min Confidence
0.85/0.90/0.95/1.00, across all 4 variants (quant only / quant+VWAP /
quant+chart / current system).

Usage:
    python run_variant_test_bs.py
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.signal_layer.volatility.realized_vol import compute_realized_vol
from engine.risk_engine.stops import check_session_end
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES

from premium_model import black_scholes_premium, days_to_next_weekly_expiry
from fyers_charges import compute_round_trip_charges

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")
VWAP_START_DATE = datetime(2025, 6, 1, tzinfo=IST)
INDEX_ID = "NIFTY50"
STRIKE_STEP = 50
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
VOL_WINDOW = 100

# Real, currently-configured LIVE risk settings for NIFTY50 (mode: both)
REAL_SETTINGS = {
    "initial_capital": 30000.0,
    "risk_per_trade_pct": 1.0,
    "hard_sl_pct": 3.5,
    "time_stop_minutes": 3,
    "tsl_activation_pct": 5.0,
    "tsl_base_trail_pct": 3.0,
    "tsl_k": 0.15,
    "tsl_floor_pct": 5.0,
}
MIN_CONFIDENCE_SWEEP = [0.85, 0.90, 0.95, 1.00]

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
    cutoff = VWAP_START_DATE.timestamp()
    filtered = [c for c in data if c["epoch"] >= cutoff]
    log(f"Loaded {len(data)} total cached candles, filtered to {len(filtered)} from {VWAP_START_DATE.strftime('%Y-%m-%d')} onward.")
    return filtered


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


def precompute_realized_vol_series(candles: list[dict]) -> list[float]:
    """
    Identical same-day-only windowing to scripts/parameter_sweep_vwap.py's
    precompute_realized_vol_series -- fixes the confirmed real bug where
    including the prior day's close in the trailing window let a single
    overnight price gap get treated as an enormous one-minute return,
    producing wildly inflated (and wildly wrong) "volatility" readings
    that fed directly into overpriced premiums.
    """
    n = len(candles)
    closes = [c["close"] for c in candles]
    vol_series = [12.0] * n
    candle_dates = [datetime.fromtimestamp(c["epoch"], tz=IST).date() if c.get("epoch") else None for c in candles]

    for i in range(n):
        current_date = candle_dates[i]
        window_start = max(0, i - VOL_WINDOW)
        same_day_start = i
        for j in range(i, window_start - 1, -1):
            if candle_dates[j] != current_date:
                break
            same_day_start = j

        window_closes = closes[same_day_start:i + 1]
        if len(window_closes) >= 20:
            rv = compute_realized_vol(window_closes)
            vol_series[i] = rv.get("realized_vol_pct") or 12.0
    return vol_series


def precompute_datetime_series(candles: list[dict]) -> list[datetime]:
    return [datetime.fromtimestamp(c["epoch"], tz=IST) if c.get("epoch") else datetime.now(tz=IST) for c in candles]


def run_variant_bs(candles: list[dict], vwap_series: list[float | None], vol_series: list[float],
                    dt_series: list[datetime], use_chart: bool, use_vwap: bool,
                    initial_capital: float, risk_per_trade_pct: float, hard_sl_pct: float,
                    time_stop_minutes: int, tsl_activation_pct: float, tsl_base_trail_pct: float,
                    tsl_k: float, tsl_floor_pct: float, min_confidence: float) -> dict:
    """
    Same confluence-gate/entry logic as run_variant_test.py's run_variant,
    but premiums are real Black-Scholes (driven by same-day realized vol,
    actual days-to-weekly-expiry, and ATM strike selection) instead of a
    constant-delta approximation, and every trade's P&L has real Fyers
    round-trip charges deducted -- both using your own existing,
    previously-tested modules unchanged.
    """
    lot_size = LOT_SIZES.get(INDEX_ID.upper())
    trades = []
    open_trade = None
    capital = initial_capital

    for i in range(HURST_WINDOW, len(candles)):
        window = candles[max(0, i - HURST_WINDOW):i]
        closes = [c["close"] for c in window if c["close"] is not None]

        current_candle = candles[i]
        current_price = current_candle["close"]
        current_dt = dt_series[i]
        current_vol_pct = vol_series[i]
        days_left = days_to_next_weekly_expiry(current_dt)

        if open_trade is not None:
            option_type = "CE" if open_trade["direction"] == "long_call" else "PE"
            sim_premium = black_scholes_premium(
                spot=current_price, strike=open_trade["strike"], days_to_expiry=days_left,
                volatility_pct=current_vol_pct, option_type=option_type,
            )
            profit_pct = (sim_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            entry_idx = open_trade["entry_candle_index"]
            elapsed_minutes = i - entry_idx
            drop_pct = (open_trade["entry_premium"] - sim_premium) / open_trade["entry_premium"] * 100

            hard_hit = drop_pct >= hard_sl_pct
            time_hit = elapsed_minutes >= time_stop_minutes and open_trade["peak_profit_pct"] <= 0

            tsl_active = open_trade["peak_profit_pct"] >= tsl_activation_pct
            if tsl_active:
                profit_above_activation = open_trade["peak_profit_pct"] - tsl_activation_pct
                trail_distance = max(tsl_floor_pct, tsl_base_trail_pct - tsl_k * profit_above_activation)
                locked_floor = open_trade["peak_profit_pct"] - trail_distance
                tsl_hit = profit_pct <= locked_floor
            else:
                tsl_hit = False

            session_end_hit = check_session_end(current_dt)["triggered"]

            if session_end_hit or hard_hit or time_hit or tsl_hit:
                reason = ("session_end_exit" if session_end_hit else
                          "hard_sl_hit" if hard_hit else
                          "time_stop" if time_hit else "tsl_hit")
                gross_pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                charges = compute_round_trip_charges(
                    entry_premium=open_trade["entry_premium"], exit_premium=sim_premium,
                    lots=open_trade["lots"], lot_size=lot_size,
                )
                net_pnl = gross_pnl - charges["total_charges"]
                capital += net_pnl
                trades.append({
                    "entry_time": open_trade["entry_time"], "exit_reason": reason,
                    "gross_pnl": round(gross_pnl, 2), "charges": charges["total_charges"],
                    "pnl": round(net_pnl, 2), "pnl_pct": round(profit_pct, 2),
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
            chart_result = {"verdict": "aligned", "confirmed": True}

        vwap_result = None
        if use_vwap:
            vwap_at_i = vwap_series[i]
            if vwap_at_i is not None:
                vwap_result = {"price": current_price, "vwap": vwap_at_i}

        gate_result = evaluate_confluence(
            quant_result, chart_result, min_confidence_threshold=min_confidence,
            vwap_result=vwap_result, require_vwap=use_vwap,
        )
        if not gate_result["fired"]:
            continue

        strike = round(current_price / STRIKE_STEP) * STRIKE_STEP
        option_type = "CE" if gate_result["direction"] == "long_call" else "PE"
        entry_premium = black_scholes_premium(
            spot=current_price, strike=strike, days_to_expiry=days_left,
            volatility_pct=current_vol_pct, option_type=option_type,
        )

        sizing = compute_position_size(
            available_capital=capital, index_id=INDEX_ID, premium_per_share=entry_premium,
            hard_sl_pct=max(hard_sl_pct, 0.5), risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1 or sizing["lots"] > 500:
            continue

        open_trade = {
            "entry_time": current_dt.strftime("%Y-%m-%d %H:%M:%S"), "entry_candle_index": i,
            "direction": gate_result["direction"], "entry_premium": entry_premium,
            "lots": sizing["lots"], "peak_profit_pct": 0.0, "strike": strike,
        }

    return _summarize(trades, initial_capital)


def _summarize(trades: list[dict], initial_capital: float) -> dict:
    if not trades:
        return {"trade_count": 0, "win_rate": None, "total_pnl": 0.0, "return_pct": 0.0,
                 "max_drawdown_pct": 0.0, "total_charges": 0.0}

    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    total_charges = sum(t["charges"] for t in trades)

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
        "total_charges": round(total_charges, 2),
    }


def main():
    log("=" * 78)
    log("4-VARIANT CONFLUENCE TEST — BLACK-SCHOLES PREMIUM MODEL, REAL FYERS CHARGES")
    log("NIFTY50, real live risk settings, mid-2025 onward (real cached candles)")
    log("=" * 78)

    candles = load_candles()
    if len(candles) < HURST_WINDOW + VOL_WINDOW:
        log(f"ERROR: insufficient candles.")
        sys.exit(1)

    log("Precomputing VWAP, realized volatility, and datetime series (shared across all runs)...")
    vwap_series = compute_vwap_series(candles)
    vol_series = precompute_realized_vol_series(candles)
    dt_series = precompute_datetime_series(candles)

    print("\n" + "=" * 120)
    print("REAL LIVE SETTINGS TEST (Black-Scholes premiums + real Fyers charges) — NIFTY50")
    print(f"Hard SL {REAL_SETTINGS['hard_sl_pct']}%, Time Stop {REAL_SETTINGS['time_stop_minutes']}min, "
          f"Risk/Trade {REAL_SETTINGS['risk_per_trade_pct']}%, Capital Rs {REAL_SETTINGS['initial_capital']:,.0f}, require_vwap=True")
    print("=" * 120)

    for min_conf in MIN_CONFIDENCE_SWEEP:
        log(f"\nRunning Min Confidence: {min_conf} ...")
        print(f"\n--- Min Confidence: {min_conf} ---")
        print(f"{'Variant':<28} {'Trades':<9} {'Win Rate':<11} {'Net P&L':<14} {'Return %':<11} {'Max DD %':<10} {'Total Charges'}")
        print("-" * 110)
        for label, cfg in VARIANTS.items():
            result = run_variant_bs(
                candles, vwap_series, vol_series, dt_series,
                use_chart=cfg["use_chart"], use_vwap=cfg["use_vwap"],
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
            marker = " <-- REAL LIVE CONFIG" if label.startswith("4.") else ""
            wr = f"{result['win_rate']}%" if result['win_rate'] is not None else "N/A"
            print(f"{label:<28} {result['trade_count']:<9} {wr:<11} {result['total_pnl']:<14,.2f} "
                  f"{result['return_pct']:<11} {result['max_drawdown_pct']:<10} {result['total_charges']:,.2f}{marker}")

    print("\nNote: Variant 4 (Current system, all legs) with require_vwap=True is your REAL live")
    print("configuration exactly as it runs today. Premiums are Black-Scholes (same-day realized")
    print("vol as IV proxy), with real Fyers brokerage/STT/exchange/SEBI/stamp/GST charges deducted")
    print("from every trade -- still a model, not literal historical option data (which does not")
    print("exist for expired contracts on Fyers or any free/API-accessible source, confirmed).")


if __name__ == "__main__":
    main()
