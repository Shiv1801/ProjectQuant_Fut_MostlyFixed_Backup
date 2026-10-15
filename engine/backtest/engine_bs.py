"""
Black-Scholes-priced backtest engine, added 2026-09-30.

WHY THIS EXISTS (separate from engine/backtest/engine.py's run_backtest):
that function approximates option premium with a constant-delta LINEAR
model seeded from a fixed approx_entry_premium (default Rs 60), which is
never re-derived from the real strike/expiry/spot at each entry. Combined
with position_sizing.py's capital-based lot clamp, this produces
near-100%-of-capital sizing on every trade regardless of the option's
real price level, and confirmed to produce a fabricated ~-97% "return"
over a 2-year run that reflects this sizing artifact, not the strategy's
real behavior. This is a NEW, ADDITIVE engine, not a replacement -- the
original run_backtest() and everything that calls it (scripts/backtest_sweep_nifty.py,
api/main.py's /backtest/run endpoint) is UNTOUCHED and still works exactly
as before. This module is only used by scripts/backtest_gate_legs_nifty_bs.py.

Every entry now:
  1. picks the REAL ATM strike (nearest 50-point strike to spot, matching
     live strike selection in api/main.py)
  2. computes the REAL time-to-expiry to the next NIFTY weekly Thursday
     expiry from the real candle timestamp
  3. estimates volatility from REALIZED historical volatility (rolling
     window of the same candles being backtested -- see bs_pricer.py's
     module docstring for the full, honest accuracy statement: strike/
     expiry/spot are exact, sigma is a realized-vol proxy, standard
     practice absent stored historical IV)
  4. prices the option via real Black-Scholes at both entry and every
     subsequent candle while the position is open (re-priced against the
     CURRENT spot and shrinking time-to-expiry every candle -- a real
     option's premium decays with time even if spot doesn't move, unlike
     the old linear-delta model which had no time decay at all)

FULL PER-TRADE LOGGING: every trade dict returned includes entry/exit
timestamps, spot price, strike, time-to-expiry at entry, the realized-vol
estimate used, entry/exit premium, lots, capital before/after, and exit
reason -- nothing is summarized away, so scripts/backtest_gate_legs_nifty_bs.py
can dump a complete, auditable log of every single trade.
"""
from datetime import datetime

from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES
from engine.backtest.bs_pricer import (
    nearest_atm_strike, next_weekly_expiry, estimate_realized_vol, black_scholes_price,
)

VOL_WINDOW = 375 * 5  # ~5 trading days of 1-min candles for the realized-vol estimate;
# long enough to smooth out single-day noise, short enough to reflect the
# CURRENT vol regime rather than a stale 2-year-old average.


def _parse_ts(bucket: str) -> datetime:
    # minute_bucket format confirmed in config/db.py: "YYYY-MM-DD HH:MM:00"
    return datetime.strptime(bucket, "%Y-%m-%d %H:%M:%S")


def _compute_vwap_series(candles: list[dict]) -> list[float | None]:
    n = len(candles)
    vwap = [None] * n
    if n == 0:
        return vwap
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    current_day = None
    for i, c in enumerate(candles):
        bucket = c.get("minute_bucket") or c.get("timestamp")
        candle_day = bucket[:10] if bucket else None
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


def run_backtest_bs(candles: list[dict], index_id: str, initial_capital: float = 30000,
                     risk_per_trade_pct: float = 3.0, hard_sl_pct: float = 3.0,
                     time_stop_minutes: int = 7, tsl_activation_pct: float = 3.0,
                     tsl_base_trail_pct: float = 3.0, tsl_k: float = 0.15, tsl_floor_pct: float = 1.5,
                     min_confidence: float = 0.85, hurst_window: int = 100, zscore_window: int = 20,
                     require_chart_confirmation: bool = True, require_vwap: bool = False,
                     log_all_trades: bool = True) -> dict:
    """
    Same confluence-gate/risk-engine logic as run_backtest() in engine.py,
    but option premium comes from real Black-Scholes pricing (see module
    docstring) instead of a flat linear-delta approximation. Every trade
    is fully logged when log_all_trades=True (default).
    """
    if len(candles) < max(hurst_window, VOL_WINDOW) + 20:
        return {"trades": [], "summary": None,
                "note": f"insufficient candles (need at least {max(hurst_window, VOL_WINDOW) + 20}, have {len(candles)})"}

    lot_size = LOT_SIZES.get(index_id.upper())
    if lot_size is None:
        return {"trades": [], "summary": None, "note": f"unknown index_id '{index_id}'"}

    vwap_series = _compute_vwap_series(candles) if require_vwap else None
    all_closes = [c["close"] for c in candles]

    trades = []
    open_trade = None
    capital = initial_capital
    skipped_no_vol = 0  # count of candles where realized vol couldn't be computed yet
    zero_volume_candles = sum(1 for c in candles if not (c.get("volume") or 0))

    for i in range(max(hurst_window, VOL_WINDOW), len(candles)):
        window = candles[max(0, i - hurst_window):i]
        closes = [c["close"] for c in window if c["close"] is not None]

        current_candle = candles[i]
        current_price = current_candle["close"]
        bucket = current_candle.get("minute_bucket") or current_candle.get("timestamp") or str(i)
        try:
            current_ts_dt = _parse_ts(bucket)
        except (ValueError, TypeError):
            current_ts_dt = None

        vol_window_closes = all_closes[max(0, i - VOL_WINDOW):i + 1]
        sigma = estimate_realized_vol(vol_window_closes)
        if sigma is None:
            skipped_no_vol += 1
            continue

        # --- Manage an open trade first ---
        if open_trade is not None:
            if current_ts_dt is not None:
                remaining_years = max(0.0, (open_trade["expiry_dt"] - current_ts_dt).total_seconds() / (365.25 * 24 * 3600))
            else:
                remaining_years = open_trade["time_to_expiry_years"]  # fallback: no decay if timestamp unparseable

            # INTRABAR SLIPPAGE FIX (added 2026-09-30): the original version
            # here only priced the option once, off the candle's CLOSE. A
            # real spot move within that 1-minute candle -- between its
            # open and its high/low -- could blow straight through the
            # hard SL/TSL threshold and back before the close, meaning the
            # close-only check would show no trigger even though a real,
            # continuously-monitored position (which is what position_monitor_loop
            # actually does live, checking every 0.5s) would have exited
            # mid-candle. This was confirmed as a real, measured effect:
            # a run with hard_sl_pct=3 produced an ACTUAL avg_loss_pct of
            # 8.55% -- nearly 3x worse than configured -- which is the
            # signature of a stop being blown through between checks, not
            # of the stop logic itself being wrong.
            #
            # Fix: price the option at BOTH the candle's high and low spot
            # (whichever is worse for this position's direction is checked
            # FIRST, as the conservative convention), and use whichever
            # premium is more adverse to evaluate SL/TSL/time-stop. This
            # bounds the worst realistic exit to what the candle's own
            # high-low range actually shows, instead of only its close.
            #
            # HONEST REMAINING LIMITATION: OHLC candles don't record the
            # ORDER in which the high and low occurred within the minute
            # -- checking the adverse extreme first is the standard,
            # conservative convention (assume the worst plausible path),
            # not a claim that this is the exact real intra-minute path.
            # This is still a closer bound to reality than close-only
            # checking, which is why it's used here.
            adverse_spot = current_candle["low"] if open_trade["option_type"] == "CE" else current_candle["high"]
            favorable_spot = current_candle["high"] if open_trade["option_type"] == "CE" else current_candle["low"]

            adverse_premium = black_scholes_price(
                S=adverse_spot, K=open_trade["strike"], T_years=remaining_years,
                sigma=sigma, option_type=open_trade["option_type"],
            )
            favorable_premium = black_scholes_price(
                S=favorable_spot, K=open_trade["strike"], T_years=remaining_years,
                sigma=sigma, option_type=open_trade["option_type"],
            )
            close_premium = black_scholes_price(
                S=current_price, K=open_trade["strike"], T_years=remaining_years,
                sigma=sigma, option_type=open_trade["option_type"],
            )

            # Peak profit updates off the FAVORABLE extreme first (a real
            # trade's peak could have been touched before any adverse move
            # reversed it) -- this is what TSL's "peak_profit_pct" is
            # supposed to track.
            favorable_profit_pct = (favorable_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], favorable_profit_pct)

            entry_idx = open_trade["entry_candle_index"]
            elapsed_minutes = i - entry_idx

            adverse_profit_pct = (adverse_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100

            hard_check = check_hard_stop_loss(
                entry_price=open_trade["entry_premium"], current_price=adverse_premium,
                entry_ts=datetime.min, now=datetime.min,
                premium_drop_pct=hard_sl_pct, time_stop_minutes=999999,
                has_moved_favorably=open_trade["peak_profit_pct"] > 0,
            )
            time_triggered = elapsed_minutes >= time_stop_minutes and open_trade["peak_profit_pct"] <= 0
            expiry_triggered = remaining_years <= 0

            tsl_check = update_trailing_stop(
                peak_profit_pct=open_trade["peak_profit_pct"], current_profit_pct=adverse_profit_pct,
                activation_pct=tsl_activation_pct, base_trail_pct=tsl_base_trail_pct,
                k=tsl_k, floor_pct=tsl_floor_pct,
            )

            should_close = hard_check["triggered"] or time_triggered or tsl_check["triggered"] or expiry_triggered
            if should_close:
                if expiry_triggered:
                    reason = "expiry"
                    exit_premium = close_premium
                    exit_profit_pct = (close_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
                elif hard_check["triggered"] or tsl_check["triggered"]:
                    reason = "hard_sl_hit" if hard_check["triggered"] else "tsl_hit"
                    # Exit AT the adverse premium that actually triggered
                    # the stop -- this is the realistic fill, not the
                    # candle's close (which may have partly recovered).
                    exit_premium = adverse_premium
                    exit_profit_pct = adverse_profit_pct
                else:
                    reason = "time_stop"
                    exit_premium = close_premium
                    exit_profit_pct = (close_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100

                pnl = (exit_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                capital_before = capital
                capital += pnl

                exit_spot = adverse_spot if reason in ("hard_sl_hit", "tsl_hit") else current_price

                trade_record = {
                    "trade_num": len(trades) + 1,
                    "entry_time": open_trade["entry_time"], "exit_time": bucket,
                    "direction": open_trade["direction"], "option_type": open_trade["option_type"],
                    "strike": open_trade["strike"],
                    "entry_spot": round(open_trade["entry_spot"], 2), "exit_spot": round(exit_spot, 2),
                    "entry_premium": round(open_trade["entry_premium"], 2), "exit_premium": round(exit_premium, 2),
                    "entry_sigma_annualized_pct": round(open_trade["sigma"] * 100, 2),
                    "days_to_expiry_at_entry": round(open_trade["time_to_expiry_years"] * 365.25, 2),
                    "lots": open_trade["lots"], "lot_size": lot_size,
                    "exit_reason": reason,
                    "pnl": round(pnl, 2), "pnl_pct": round(exit_profit_pct, 2),
                    "peak_profit_pct": round(open_trade["peak_profit_pct"], 2),
                    "hold_minutes": elapsed_minutes,
                    "capital_before": round(capital_before, 2), "capital_after": round(capital, 2),
                    "quant_confidence": open_trade["quant_confidence"],
                    "chart_confirmed": open_trade["chart_confirmed"],
                    "vwap_confirmed": open_trade["vwap_confirmed"],
                }
                trades.append(trade_record)
                open_trade = None
            continue

        if capital <= 0:
            # Capital exhausted -- no further entries possible, matches
            # real account behavior (can't size a position with zero or
            # negative capital). Stated explicitly rather than silently
            # producing lots=0 entries that clutter the log with no-ops.
            continue

        # --- No open trade: evaluate the gate for a new entry ---
        hurst_result = compute_hurst_exponent(closes)
        zscore_result = compute_zscore(closes[-zscore_window:] if len(closes) >= zscore_window else closes, window=zscore_window)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)

        if not quant_result.get("direction"):
            continue

        chart_candles = window + [current_candle]
        chart_result = confirm_chart_structure(chart_candles, quant_result["direction"])

        vwap_result = None
        if require_vwap:
            current_vwap = vwap_series[i]
            vwap_result = {"price": current_price, "vwap": current_vwap}

        gate_result = evaluate_confluence(
            quant_result, chart_result, min_confidence_threshold=min_confidence,
            vwap_result=vwap_result, require_vwap=require_vwap, require_chart=require_chart_confirmation,
        )
        if not gate_result["fired"]:
            continue

        if current_ts_dt is None:
            continue  # can't compute real expiry without a real timestamp -- skip rather than fabricate

        direction = gate_result["direction"]
        option_type = "CE" if direction == "long_call" else "PE"
        strike = nearest_atm_strike(current_price)
        expiry_dt = next_weekly_expiry(current_ts_dt)
        time_to_expiry_years = max(1e-6, (expiry_dt - current_ts_dt).total_seconds() / (365.25 * 24 * 3600))

        entry_premium = black_scholes_price(
            S=current_price, K=strike, T_years=time_to_expiry_years, sigma=sigma, option_type=option_type,
        )

        sizing = compute_position_size(
            available_capital=capital, index_id=index_id, premium_per_share=entry_premium,
            hard_sl_pct=hard_sl_pct, risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue

        open_trade = {
            "entry_time": bucket, "entry_candle_index": i, "direction": direction, "option_type": option_type,
            "entry_spot": current_price, "strike": strike, "expiry_dt": expiry_dt,
            "time_to_expiry_years": time_to_expiry_years, "sigma": sigma,
            "entry_premium": entry_premium, "lots": sizing["lots"], "peak_profit_pct": 0.0,
            "quant_confidence": round(quant_result.get("confidence", 0.0), 4),
            "chart_confirmed": gate_result.get("chart_confirmed"),
            "vwap_confirmed": gate_result.get("vwap_confirmed"),
        }

    summary = _summarize_trades(trades, initial_capital)
    return {
        "trades": trades if log_all_trades else [],
        "trade_count_actual": len(trades),
        "summary": summary,
        "diagnostics": {
            "candles_used": len(candles),
            "candles_skipped_insufficient_vol_history": skipped_no_vol,
            "candles_with_zero_volume": zero_volume_candles,
            "zero_volume_pct": round(zero_volume_candles / len(candles) * 100, 2) if candles else None,
        },
        "gate_config": {"require_chart_confirmation": require_chart_confirmation, "require_vwap": require_vwap,
                         "min_confidence": min_confidence},
        "note": "Black-Scholes-priced backtest: strike (real ATM), expiry (real weekly Thursday cycle), and "
                "spot (real candle data) are exact; volatility is a realized-vol proxy estimated from the same "
                "candle history (no historical IV is stored) -- see engine/backtest/bs_pricer.py for the full "
                "accuracy statement. If zero_volume_pct above is high, VWAP-gated results (require_vwap=True) "
                "are running with that leg mostly SKIPPED (not vetoing), not genuinely enforced -- check this "
                "before trusting a VWAP comparison.",
    }


def _summarize_trades(trades: list[dict], initial_capital: float) -> dict:
    if not trades:
        return {"trade_count": 0, "win_rate": None, "total_pnl": 0.0, "final_capital": initial_capital,
                "return_pct": 0.0, "max_drawdown_pct": 0.0, "avg_win_pct": None, "avg_loss_pct": None}

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

    exit_reason_counts = {}
    for t in trades:
        exit_reason_counts[t["exit_reason"]] = exit_reason_counts.get(t["exit_reason"], 0) + 1

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "total_pnl": round(total_pnl, 2),
        "final_capital": round(initial_capital + total_pnl, 2),
        "return_pct": round(total_pnl / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(abs(t["pnl_pct"]) for t in trades if t["pnl"] <= 0) / max(1, len(trades) - len(wins)), 2) if len(trades) > len(wins) else None,
        "exit_reason_breakdown": exit_reason_counts,
        "avg_hold_minutes": round(sum(t["hold_minutes"] for t in trades) / len(trades), 1),
    }
