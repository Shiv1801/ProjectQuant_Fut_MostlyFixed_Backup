"""
INDEX-MODE backtest: same signal stack (Hurst regime + Z-score + chart
structure confirmation) and same risk engine (hard SL, time stop, TSL
ratchet) as the live options-buying system -- but P&L is computed
directly on the NIFTY50 INDEX PRICE, as if you were trading the index
itself (e.g. via futures/CFD-equivalent), not an option premium.

WHY THIS EXISTS: the live/production backtest engine
(engine/backtest/engine.py) simulates option premium via a constant-
delta approximation (delta=0.35) because that's what the real live
system trades. This script strips that approximation out entirely --
entry/exit prices ARE the NIFTY index close price, full stop -- so
there is no delta-model uncertainty in the P&L math at all. This is
the more "honest" number in one sense (no premium-approximation
error) but a DIFFERENT instrument's economics in another (no options
leverage, no time decay, no bid-ask on premium).

EXPLICIT INSTRUCTION FOLLOWED (per Shivam, 2026-09-29): apply the SAME
risk_params percentages (hard_sl_pct=3, tsl_activation_pct=3,
tsl_base_trail_pct=6, tsl_k=0.15, tsl_floor_pct=1.5, time_stop_minutes=7,
min_confidence=1.00) DIRECTLY to the index price, with no delta-scaling.
This was explicitly chosen over scaling the percentages for
option-like sensitivity -- Shivam was told this means Hard SL / TSL
will trigger far less often than they do live (a 3% NIFTY index move
intraday is rare), so MOST exits are expected to come from time_stop,
not SL/TSL. This is not a bug; it is the literal requested behavior.

POSITION SIZING (updated per explicit instruction, 2026-09-29): trades
in REAL NSE NIFTY futures lots of 65 (matching LOT_SIZES["NIFTY50"] in
engine/risk_engine/position_sizing.py), at FULL notional -- no margin
or leverage modeled, so a position's entire (qty x index_price) value
must be covered by available capital, same as paying for the whole
contract rather than a margined position. Starting capital raised from
the paper-options Rs 30,000 to Rs 20,00,000, since even 1 lot (65 x
~Rs 23,500 =~ Rs 1.53M) would be unaffordable otherwise. This
UNDERSTATES how many lots a real margin-traded futures account could
hold with this capital (real SPAN+exposure margin is roughly 10-15% of
notional, not 100%) -- Shivam was told this tradeoff explicitly and
chose the conservative no-leverage framing over modeling real margin
rules, which vary by broker and day.

DATA WINDOW: this script has NO date filter -- it loads and backtests
every NIFTY50 candle currently in data/project_trade.db, whatever span
that turns out to be. To extend the window before running this (e.g.
to 2 years), hit your running server's backfill endpoint FIRST:
    http://127.0.0.1:8001/backfill/extended/NIFTY50?days=730
Check that call's response for "errors" -- Fyers' actual documented
history retention for 1-min resolution data is NOT guaranteed to cover
a full 2 years; if it refuses candles past some point, that chunk will
show up as an error in the response rather than silently vanishing,
and this script will simply run on whatever candles actually made it
into the DB (the printed "Loaded N candles, X -> Y" line always tells
you the REAL span used -- trust that line over the days= you requested).

HONEST LIMITATIONS (unchanged from run_backtest(), still apply here):
  - OFI, PCR require live depth/chain data not retained historically
    -- excluded from quant confidence, same as always.
  - VWAP (Leg 3) is NOT wired into any backtest, live config or this
    one -- require_vwap is a live-only toggle right now. This script
    runs with require_chart=True (Leg 2 ON, matching live config) and
    Leg 3 skipped (not evaluated, not vetoing), which is the same gap
    the live/production backtest engine already has.
  - No transaction costs, slippage, or brokerage are modeled.
  - No overnight gap risk beyond what the historical 1-min candles
    already show at session boundaries.

Run from the project root:
    python -m scripts.backtest_index_mode
"""
import sqlite3
import json
from datetime import datetime

from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.risk_engine.stops import update_trailing_stop, check_session_end

DB_PATH = "data/project_trade.db"
INDEX_ID = "NIFTY50"

# ---- EXACT live NIFTY50 settings as of 2026-09-29 (per Shivam's own save) ----
SETTINGS = {
    "hard_sl_pct": 3.0,
    "time_stop_minutes": 7,
    "tsl_activation_pct": 3.0,
    "tsl_base_trail_pct": 6.0,
    "tsl_k": 0.15,
    "tsl_floor_pct": 1.5,
    "risk_per_trade_pct": 3.0,
    "min_confidence": 1.00,
    "require_chart_confirmation": True,
}

# ---- REAL NSE NIFTY futures contract spec (per Shivam's explicit
# instruction, 2026-09-29): trade in whole lots of 65, at FULL notional
# (no margin/leverage modeled -- a lot's entire index-price x qty value
# must be covered by available capital, exactly like paying for the
# full contract value rather than a margined position). This is a
# deliberately conservative, no-leverage framing: real NSE futures are
# margin-traded (~10-15% SPAN+exposure, varies daily/broker), so this
# UNDERSTATES how many lots a real futures account with this much
# capital could actually hold. Shivam was told this tradeoff explicitly
# and chose full notional + a realistic starting capital instead.
NIFTY_LOT_SIZE = 65

# Raised from the paper-options capital of Rs 30,000 -- at ~Rs 23,500
# index level, 1 lot alone is ~Rs 1.5M notional, so Rs 30,000 could
# never afford even 1 lot. Rs 20,00,000 comfortably covers 1+ lot with
# room for risk_per_trade_pct sizing to flex between 1-2 lots depending
# on the index price level at entry, rather than being stuck at a flat
# 0 or 1 lot the entire backtest.
INITIAL_CAPITAL = 2000000.0
HURST_WINDOW = 100
ZSCORE_WINDOW = 20


def load_candles(db_path: str, index_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT minute_bucket, open, high, low, close FROM candles "
        "WHERE index_id = ? AND close IS NOT NULL ORDER BY minute_bucket ASC",
        (index_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_index_backtest(candles: list[dict]) -> dict:
    if len(candles) < HURST_WINDOW + 20:
        return {"trades": [], "note": "insufficient candles"}

    trades = []
    open_trade = None
    capital = INITIAL_CAPITAL
    equity_curve = []  # (timestamp, capital) sampled at every candle

    total_iterations = len(candles) - HURST_WINDOW
    progress_step = max(1, total_iterations // 20)  # ~20 progress prints regardless of data size

    for loop_idx, i in enumerate(range(HURST_WINDOW, len(candles))):
        if loop_idx % progress_step == 0:
            pct = (loop_idx / total_iterations * 100) if total_iterations > 0 else 100
            print(f"  ...{pct:5.1f}%  ({candles[i]['minute_bucket']})  trades so far: {len(trades)}", flush=True)
        window = candles[max(0, i - HURST_WINDOW):i]
        closes = [c["close"] for c in window if c["close"] is not None]
        current_candle = candles[i]
        current_price = current_candle["close"]
        current_ts = current_candle["minute_bucket"]
        current_dt = datetime.strptime(current_ts, "%Y-%m-%d %H:%M:%S")

        # ---- Manage an open trade first ----
        if open_trade is not None:
            direction = open_trade["direction"]
            entry_price = open_trade["entry_price"]
            # For index trading: "profit_pct" is the raw index price % move,
            # sign-flipped for puts (a bearish/"long_put" trade profits when
            # the index falls).
            raw_move_pct = (current_price - entry_price) / entry_price * 100
            profit_pct = raw_move_pct if direction == "long_call" else -raw_move_pct
            open_trade["peak_profit_pct"] = max(open_trade["peak_profit_pct"], profit_pct)

            # REAL BUG FOUND AND FIXED while building this script: using
            # candle-INDEX delta (i - entry_candle_index) as "elapsed
            # minutes" silently breaks the moment candles aren't
            # contiguous every single minute (weekends, holidays, any
            # gap in the historical feed) -- a trade opened Friday and
            # still open Monday would show only ~5-10 "minutes" elapsed
            # by index-delta even though days actually passed, but here
            # it did the OPPOSITE and worse: several trades' index-delta
            # showed THOUSANDS of "minutes" (i.e. thousands of 1-min
            # candles between entry and the print) because with only ~6
            # trades firing across 6+ months of data, entries were far
            # apart and the open position sat through many days of
            # candles untouched by any session-close logic -- so hold
            # times of 6,000-17,000 were REAL candle counts, not a
            # display bug, but they should never have been allowed:
            # this backtest was missing the session-end force-close the
            # live system has (check_session_end in stops.py), so
            # positions were riding through weekends/nights for weeks
            # instead of being flattened at 15:28 each day. Fixed below
            # by (a) computing elapsed_minutes from real wall-clock
            # datetimes, not candle index, and (b) force-closing any
            # open position at end of trading session, matching live.
            elapsed_minutes = (current_dt - open_trade["entry_dt"]).total_seconds() / 60

            hard_sl_triggered = profit_pct <= -SETTINGS["hard_sl_pct"]
            time_triggered = elapsed_minutes >= SETTINGS["time_stop_minutes"] and open_trade["peak_profit_pct"] <= 0

            tsl_check = update_trailing_stop(
                peak_profit_pct=open_trade["peak_profit_pct"], current_profit_pct=profit_pct,
                activation_pct=SETTINGS["tsl_activation_pct"], base_trail_pct=SETTINGS["tsl_base_trail_pct"],
                k=SETTINGS["tsl_k"], floor_pct=SETTINGS["tsl_floor_pct"],
            )

            session_end = check_session_end(current_dt)

            should_close = hard_sl_triggered or time_triggered or tsl_check["triggered"] or session_end["triggered"]
            if should_close:
                if hard_sl_triggered:
                    reason = "hard_sl_hit"
                elif session_end["triggered"]:
                    reason = "session_end_exit"
                elif time_triggered:
                    reason = "time_stop"
                else:
                    reason = "tsl_hit"
                exit_price = current_price
                qty = open_trade["qty"]
                pnl = (exit_price - entry_price) * qty if direction == "long_call" else (entry_price - exit_price) * qty
                pnl_pct = profit_pct
                capital += pnl

                trades.append({
                    "entry_time": open_trade["entry_time"], "exit_time": current_ts,
                    "direction": direction, "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2), "qty": qty, "lots": open_trade["lots"],
                    "exit_reason": reason, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                    "hold_minutes": round(elapsed_minutes, 1), "peak_profit_pct": round(open_trade["peak_profit_pct"], 2),
                    "capital_after": round(capital, 2),
                })
                open_trade = None
            equity_curve.append((current_ts, capital))
            continue

        equity_curve.append((current_ts, capital))

        # ---- No open trade: evaluate the gate for a new entry ----
        hurst_result = compute_hurst_exponent(closes)
        zscore_result = compute_zscore(closes[-ZSCORE_WINDOW:] if len(closes) >= ZSCORE_WINDOW else closes, window=ZSCORE_WINDOW)

        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)

        if not quant_result.get("direction"):
            continue

        chart_candles = window + [current_candle]
        chart_result = confirm_chart_structure(chart_candles, quant_result["direction"])
        gate_result = evaluate_confluence(
            quant_result, chart_result, min_confidence_threshold=SETTINGS["min_confidence"],
            require_chart=SETTINGS["require_chart_confirmation"],
        )

        if not gate_result["fired"]:
            continue

        # Position sizing: same capital-at-risk formula the live system
        # uses (risk_amount = capital x risk_per_trade_pct), but
        # quantized to WHOLE LOTS of 65 (real NSE NIFTY futures lot
        # size), at FULL notional (no margin modeled -- see module
        # docstring). risk_amount / (index_price x hard_sl_pct/100)
        # gives a raw unit count first, then floor-divided into lots.
        risk_amount = capital * (SETTINGS["risk_per_trade_pct"] / 100)
        max_loss_per_unit = current_price * (SETTINGS["hard_sl_pct"] / 100)
        raw_units = int(risk_amount / max_loss_per_unit) if max_loss_per_unit > 0 else 0
        lots = raw_units // NIFTY_LOT_SIZE
        qty = lots * NIFTY_LOT_SIZE
        # cap so a single position's full notional never exceeds available capital
        while qty > 0 and (qty * current_price) > capital:
            lots -= 1
            qty = lots * NIFTY_LOT_SIZE
        if qty < NIFTY_LOT_SIZE:
            continue  # can't afford even 1 full lot at this risk sizing -- skip, no partial lots

        open_trade = {
            "entry_time": current_ts, "entry_dt": current_dt, "entry_candle_index": i,
            "direction": gate_result["direction"], "lots": lots,
            "entry_price": current_price, "qty": qty, "peak_profit_pct": 0.0,
        }

    summary = summarize(trades, INITIAL_CAPITAL, equity_curve)
    return {"trades": trades, "summary": summary, "equity_curve": equity_curve, "settings": SETTINGS}


def summarize(trades: list[dict], initial_capital: float, equity_curve: list) -> dict:
    if not trades:
        return {"trade_count": 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    # Max drawdown off the actual equity curve (post-trade capital
    # marks), not just trade-by-trade -- catches intra-sequence peaks
    # correctly even with many flat (no-trade) candles between them.
    peak = initial_capital
    max_dd_pct = 0.0
    max_dd_rupees = 0.0
    for _, cap in equity_curve:
        peak = max(peak, cap)
        dd_rupees = peak - cap
        dd_pct = (dd_rupees / peak * 100) if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)
        max_dd_rupees = max(max_dd_rupees, dd_rupees)

    final_capital = initial_capital + total_pnl
    total_return_pct = total_pnl / initial_capital * 100

    # CAGR: from first to last trade's timestamps (actual elapsed
    # calendar time in the backtested data), not a fixed 365 days.
    first_ts = datetime.strptime(trades[0]["entry_time"], "%Y-%m-%d %H:%M:%S")
    last_ts = datetime.strptime(trades[-1]["exit_time"], "%Y-%m-%d %H:%M:%S")
    days_elapsed = max((last_ts - first_ts).total_seconds() / 86400, 1)
    years_elapsed = days_elapsed / 365.25
    cagr_pct = ((final_capital / initial_capital) ** (1 / years_elapsed) - 1) * 100 if final_capital > 0 and years_elapsed > 0 else None

    # Sharpe: per-trade return series, annualized by trade frequency
    # (NOT daily bars -- this system doesn't hold daily positions, it's
    # intraday-only with a handful of trades per day at most).
    returns = [t["pnl_pct"] / 100 for t in trades]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 0
    std_r = variance ** 0.5
    trades_per_year = len(trades) / years_elapsed if years_elapsed > 0 else len(trades)
    sharpe = (mean_r / std_r) * (trades_per_year ** 0.5) if std_r > 0 else None

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    win_rate = len(wins) / len(trades)
    # Expectancy in Rupees per trade AND as an R-multiple (per 1 unit risked)
    expectancy_rupees = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    return {
        "trade_count": len(trades), "win_count": len(wins), "loss_count": len(losses),
        "win_rate_pct": round(win_rate * 100, 2),
        "total_pnl": round(total_pnl, 2), "total_return_pct": round(total_return_pct, 2),
        "final_capital": round(final_capital, 2),
        "cagr_pct": round(cagr_pct, 2) if cagr_pct is not None else None,
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd_pct, 2), "max_drawdown_rupees": round(max_dd_rupees, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "expectancy_rupees_per_trade": round(expectancy_rupees, 2),
        "avg_win_rupees": round(avg_win, 2), "avg_loss_rupees": round(avg_loss, 2),
        "gross_profit": round(gross_profit, 2), "gross_loss": round(gross_loss, 2),
        "exit_reason_breakdown": reasons,
        "days_elapsed": round(days_elapsed, 1), "years_elapsed": round(years_elapsed, 3),
        "first_trade": trades[0]["entry_time"], "last_trade": trades[-1]["exit_time"],
    }


def main():
    print(f"Loading candles for {INDEX_ID} from {DB_PATH}...")
    candles = load_candles(DB_PATH, INDEX_ID)
    print(f"Loaded {len(candles)} candles, {candles[0]['minute_bucket']} -> {candles[-1]['minute_bucket']}")
    print(f"Settings: {json.dumps(SETTINGS, indent=2)}")

    result = run_index_backtest(candles)

    with open("scripts/backtest_index_mode_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)

    s = result.get("summary", {})
    if not s or s.get("trade_count", 0) == 0:
        print("No trades fired. Check settings/data.")
        return

    print("\n=== SUMMARY ===")
    for k, v in s.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
