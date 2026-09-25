"""
Backtest Engine (PRD §6). Replays historical 1-min candles minute-by-
minute through the SAME confluence gate and risk engine logic already
built and tested for live/paper trading — no separate decision logic,
so backtest results actually reflect what the live system would do.

Known limitation (stated explicitly, not hidden): OFI and PCR require
live depth/option-chain snapshots we don't retain historically (we
only store candle closes, not full order-book/chain history). The
backtest therefore runs the quant score using only the models that ARE
derivable from price history alone — Hurst regime + Z-score — plus
chart structure confirmation. This is a real, meaningful subset (not
the full live confluence score), and every result explicitly reports
this so it's never mistaken for a full replay.
"""
from datetime import datetime

from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop
from engine.risk_engine.position_sizing import compute_position_size, LOT_SIZES


def _simulate_option_premium(entry_index_price: float, current_index_price: float,
                              direction: str, entry_premium: float, delta: float = 0.35) -> float:
    """
    Simplified premium simulation: since we only have index candles (not
    historical option-chain snapshots), premium is approximated via a
    constant-delta linear model. This is a real approximation, not exact
    option pricing (no gamma/theta/IV surface evolution) — stated plainly
    in every backtest result's methodology note.
    """
    index_move = current_index_price - entry_index_price
    if direction == "long_put":
        index_move = -index_move  # a put gains when index falls
    premium_change = index_move * delta
    return max(0.01, entry_premium + premium_change)  # premium can't go negative


def run_backtest(candles: list[dict], index_id: str, initial_capital: float = 100000,
                  risk_per_trade_pct: float = 2.0, hard_sl_pct: float = 27.0,
                  time_stop_minutes: int = 18, tsl_activation_pct: float = 15.0,
                  tsl_base_trail_pct: float = 12.0, tsl_k: float = 0.15, tsl_floor_pct: float = 5.0,
                  min_confidence: float = 0.5, hurst_window: int = 100, zscore_window: int = 20,
                  approx_entry_premium: float = 60.0, approx_delta: float = 0.35) -> dict:
    """
    candles: full chronological 1-min candle history (oldest first), each
    with at least 'close', 'high', 'low', 'open'.
    Returns full trade list + summary stats, walk-forward ready (caller
    can pre-split candles into train/test windows and call this twice).
    """
    if len(candles) < hurst_window + 20:
        return {"trades": [], "summary": None, "note": f"insufficient candles (need at least {hurst_window + 20}, have {len(candles)})"}

    lot_size = LOT_SIZES.get(index_id.upper())
    if lot_size is None:
        return {"trades": [], "summary": None, "note": f"unknown index_id '{index_id}'"}

    trades = []
    open_trade = None
    capital = initial_capital

    for i in range(hurst_window, len(candles)):
        window = candles[max(0, i - hurst_window):i]
        closes = [c["close"] for c in window if c["close"] is not None]

        current_candle = candles[i]
        current_price = current_candle["close"]
        current_ts = current_candle.get("minute_bucket") or current_candle.get("timestamp") or str(i)

        # --- Manage an open trade first ---
        if open_trade is not None:
            sim_premium = _simulate_option_premium(
                open_trade["entry_index_price"], current_price, open_trade["direction"],
                open_trade["entry_premium"], approx_delta,
            )
            profit_pct = (sim_premium - open_trade["entry_premium"]) / open_trade["entry_premium"] * 100
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            entry_idx = open_trade["entry_candle_index"]
            elapsed_minutes = i - entry_idx  # 1-min candles -> index delta == minutes

            hard_check = check_hard_stop_loss(
                entry_price=open_trade["entry_premium"], current_price=sim_premium,
                entry_ts=datetime.min, now=datetime.min,  # time handled via elapsed_minutes directly below
                premium_drop_pct=hard_sl_pct, time_stop_minutes=999999,  # disable internal time check, do it manually
                has_moved_favorably=open_trade["peak_profit_pct"] > 0,
            )
            time_triggered = elapsed_minutes >= time_stop_minutes and open_trade["peak_profit_pct"] <= 0

            tsl_check = update_trailing_stop(
                peak_profit_pct=open_trade["peak_profit_pct"], current_profit_pct=profit_pct,
                activation_pct=tsl_activation_pct, base_trail_pct=tsl_base_trail_pct,
                k=tsl_k, floor_pct=tsl_floor_pct,
            )

            should_close = hard_check["triggered"] or time_triggered or tsl_check["triggered"]
            if should_close:
                reason = "hard_sl_hit" if hard_check["triggered"] else ("time_stop" if time_triggered else "tsl_hit")
                pnl = (sim_premium - open_trade["entry_premium"]) * open_trade["lots"] * lot_size
                pnl_pct = profit_pct
                capital += pnl

                trades.append({
                    "entry_time": open_trade["entry_time"], "exit_time": current_ts,
                    "direction": open_trade["direction"], "entry_premium": round(open_trade["entry_premium"], 2),
                    "exit_premium": round(sim_premium, 2), "lots": open_trade["lots"],
                    "exit_reason": reason, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                    "hold_minutes": elapsed_minutes,
                })
                open_trade = None
            continue  # don't evaluate a new entry on the same candle we just managed

        # --- No open trade: evaluate the gate for a new entry ---
        hurst_result = compute_hurst_exponent(closes)
        zscore_result = compute_zscore(closes[-zscore_window:] if len(closes) >= zscore_window else closes, window=zscore_window)

        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)

        if not quant_result.get("direction"):
            continue

        chart_candles = window + [current_candle]
        chart_result = confirm_chart_structure(chart_candles, quant_result["direction"])
        gate_result = evaluate_confluence(quant_result, chart_result, min_confidence_threshold=min_confidence)

        if not gate_result["fired"]:
            continue

        sizing = compute_position_size(
            available_capital=capital, index_id=index_id, premium_per_share=approx_entry_premium,
            hard_sl_pct=hard_sl_pct, risk_per_trade_pct=risk_per_trade_pct,
        )
        if sizing["lots"] < 1:
            continue

        open_trade = {
            "entry_time": current_ts, "entry_candle_index": i, "direction": gate_result["direction"],
            "entry_index_price": current_price, "entry_premium": approx_entry_premium,
            "lots": sizing["lots"], "peak_profit_pct": 0.0,
        }

    summary = _summarize_trades(trades, initial_capital)
    return {
        "trades": trades, "summary": summary,
        "note": "Backtest uses price-derived signals only (Hurst + Z-score + chart structure) — "
                "OFI/PCR require live depth/chain data not retained historically, so they're excluded here. "
                "Option premium is approximated via a constant-delta model, not full options pricing.",
    }


def _summarize_trades(trades: list[dict], initial_capital: float) -> dict:
    if not trades:
        return {"trade_count": 0, "win_rate": None, "total_pnl": 0.0, "final_capital": initial_capital, "max_drawdown_pct": 0.0}

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
        "win_rate": round(len(wins) / len(trades), 4),
        "total_pnl": round(total_pnl, 2),
        "final_capital": round(initial_capital + total_pnl, 2),
        "return_pct": round(total_pnl / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(abs(t["pnl_pct"]) for t in trades if t["pnl"] <= 0) / max(1, len(trades) - len(wins)), 2) if len(trades) > len(wins) else None,
    }
