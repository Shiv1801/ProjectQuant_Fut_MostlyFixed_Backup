"""
Execution Layer — Paper Trading (PRD §5, Execution Modes). Turns a
fired confluence gate + selected strike + position size into an actual
tracked paper position, and continuously monitors open positions
against the risk engine's hard SL / time stop / trailing stop.

This runs as part of the background engine (called from the decision
loop), not from a browser request — trades open and get monitored
regardless of whether anyone has the dashboard open, per your
"runs even when I'm not looking" requirement.
"""
from datetime import datetime, timezone, timedelta

from config.db import (
    open_position, update_position_price, close_position, get_open_positions, insert_trade_log,
)
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop, check_session_end
from engine.execution.trade_dispatcher import execute_exit

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)   # 9:15 AM IST
MARKET_CLOSE = (15, 30)  # 3:30 PM IST


def market_minutes_between(start: datetime, end: datetime) -> float:
    """
    Counts elapsed minutes DURING MARKET HOURS ONLY between two
    timestamps — not raw wall-clock time, and excluding weekends. A
    position opened at 3:25 PM Friday and checked again 9:20 AM Monday
    should show ~10 trading minutes elapsed (5 min before Friday close
    + 5 min after Monday open), not ~4,000+ minutes of wall-clock time.
    This directly fixes the reported bug where trades showed impossible
    hold times (e.g. 400 minutes) that don't correspond to any real
    trading window — that number was counting nights/weekends as if
    they were live market minutes.

    Does not account for market holidays (would need a holiday
    calendar) — weekends are the main source of the reported bug and
    are handled correctly; holiday-adjacent trades may still show a
    slightly inflated but much-improved number.

    Both start and end should be timezone-aware IST datetimes (or naive,
    treated as IST — matches how the rest of the app stores timestamps).
    """
    if end <= start:
        return 0.0

    total_minutes = 0.0
    current_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)

    while current_day <= end_day:
        if current_day.weekday() < 5:  # Monday=0 ... Friday=4, Saturday/Sunday excluded
            day_open = current_day.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1])
            day_close = current_day.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])

            window_start = max(start, day_open)
            window_end = min(end, day_close)

            if window_end > window_start:
                total_minutes += (window_end - window_start).total_seconds() / 60

        current_day += timedelta(days=1)

    return round(total_minutes, 2)


def execute_paper_entry(index_id: str, direction: str, selected_strike: dict, lots: int, lot_size: int) -> dict:
    """
    Opens a new paper position from a fired signal + chosen strike + sized
    lots. Does not touch Fyers (paper mode never places real orders) —
    purely a DB write representing "if this were live, here's the trade."

    This is called UNCONDITIONALLY by the auto-entry loop regardless of
    the configured trading mode (paper/live/both) — paper tracking is
    always maintained as the permanent baseline record, per design.
    """
    entry_price = selected_strike.get("ltp")
    if not entry_price or entry_price <= 0:
        return {"opened": False, "note": "invalid entry price on selected strike"}

    position_id = open_position(
        index_id=index_id, direction=direction, option_symbol=selected_strike.get("symbol", "UNKNOWN"),
        strike=selected_strike["strike"], option_type=selected_strike["option_type"],
        lots=lots, lot_size=lot_size, entry_price=entry_price, mode="paper",
    )

    return {
        "opened": True, "position_id": position_id, "index_id": index_id, "direction": direction,
        "strike": selected_strike["strike"], "option_type": selected_strike["option_type"],
        "lots": lots, "entry_price": entry_price,
    }


def execute_live_entry(index_id: str, direction: str, selected_strike: dict, lots: int, lot_size: int) -> dict:
    """
    Placeholder for real order placement via Fyers' order API (PRD
    Execution Modes — Live). NOT YET IMPLEMENTED. Returns a clear,
    honest "not implemented" result rather than silently doing nothing
    — this matters because a trading mode setting that appears to work
    but doesn't is worse than one that visibly refuses, since a silent
    no-op could be mistaken for "no signal fired" rather than "this
    feature doesn't exist yet."
    """
    return {
        "opened": False, "mode": "live",
        "note": "Live order execution is not yet implemented. This trade was recorded in paper mode only. "
                "Building real Fyers order placement is a deliberate future step, not shipped yet.",
    }


def find_current_price_for_position(position: dict, parsed_chain: dict | None) -> float | None:
    """Looks up the live LTP for this position's exact strike/option_type from a freshly parsed chain."""
    if not parsed_chain:
        return None
    for row in parsed_chain.get("strikes", []):
        if row["strike"] == position["strike"]:
            side = row.get(position["option_type"])
            if side:
                return side.get("ltp")
    return None


def check_strike_still_tracked(position_strike: float, parsed_chain: dict | None) -> dict:
    """
    New rule: if a position's strike is no longer present in the
    currently-tracked option chain (the live set of strikes being
    fetched each cycle, e.g. 21 strikes total), force-close it
    regardless of profit or loss.

    This checks the REAL, live tracked set directly (parsed_chain["strikes"])
    rather than re-deriving an assumed ATM +/- N*step boundary — more
    robust, since it reflects exactly what Fyers is actually returning
    each cycle, not an assumption about how that range is centered.

    Once a position's strike drops out of this set, the app stops
    receiving fresh chain data (LTP, greeks, OI) for it, so it can no
    longer be priced or monitored accurately — a real risk independent
    of current profit/loss.
    """
    if not parsed_chain or not parsed_chain.get("strikes"):
        return {"triggered": False, "reason": None, "note": "chain data unavailable this cycle, skipping check"}

    tracked_strikes = {row["strike"] for row in parsed_chain["strikes"]}
    if position_strike not in tracked_strikes:
        return {"triggered": True, "reason": "strike_out_of_scope",
                "note": f"position strike {position_strike} no longer in the {len(tracked_strikes)} currently-tracked strikes"}
    return {"triggered": False, "reason": None}


def monitor_and_close_positions(get_chain_fn, get_index_risk_params_fn=None,
                                 hard_sl_pct: float = 27.0, time_stop_minutes: int = 18,
                                 tsl_activation_pct: float = 15.0, tsl_base_trail_pct: float = 12.0,
                                 tsl_k: float = 0.15, tsl_floor_pct: float = 5.0,
                                 access_token: str | None = None) -> list[dict]:
    """
    Checks every open position against hard SL / time stop / trailing
    stop, closes any that trigger, writes closed trades to trade_log.

    get_chain_fn: callable(index_id) -> parsed chain dict (so this stays
    decoupled from shared_state/API specifics — testable in isolation).

    get_index_risk_params_fn: optional callable(index_id) -> dict of
    risk params for THAT index. When provided, each position is checked
    against its OWN index's settings (Nifty positions use Nifty's SL/
    TSL, BankNifty positions use BankNifty's, etc.) — required now that
    settings are per-index, not global. When omitted, falls back to the
    flat hard_sl_pct/time_stop_minutes/... kwargs for every position
    (backward compatible with single-config callers/tests).

    access_token: needed to place a REAL sell order when closing a
    position whose mode is "live". FIXED A REAL, SERIOUS GAP found
    during this session's file-recovery audit: this function
    previously called config.db.close_position DIRECTLY for every
    automatic exit (hard SL, TSL, time-stop, session-end, strike-
    scope) regardless of mode — meaning a live position hitting an
    automatic exit was marked "closed" in our own records while the
    REAL position on Fyers was never actually sold. Now routes through
    engine.execution.trade_dispatcher.execute_exit, the same dispatcher
    already used by the manual "Exit Trade" button, so automatic and
    manual exits get identical real-order safety. If access_token is
    None (e.g. no token available this cycle), paper positions still
    close normally; a live position's exit is deferred (left open,
    checked again next cycle) rather than silently closed without a
    real order — see the two call sites below for exactly how.
    """
    actions_taken = []
    open_positions = get_open_positions()

    for position in open_positions:
        parsed_chain = get_chain_fn(position["index_id"])
        current_price = find_current_price_for_position(position, parsed_chain)

        if current_price is None:
            # Before giving up on this cycle, check whether the reason
            # we have no price is PRECISELY that the strike has fallen
            # out of the currently-tracked range -- if so, this is
            # exactly the situation the strike-scope rule exists for,
            # and we should force-close using the last known price
            # rather than silently skip forever (a position whose
            # strike never re-enters the tracked range would otherwise
            # never close through this path at all).
            strike_scope_check = check_strike_still_tracked(position["strike"], parsed_chain)
            if strike_scope_check["triggered"]:
                last_known_price = position.get("current_price") or position["entry_price"]
                pnl = (last_known_price - position["entry_price"]) * position["lots"] * position["lot_size"]
                pnl_pct = (last_known_price - position["entry_price"]) / position["entry_price"] * 100
                now = datetime.now(IST)

                exit_result = execute_exit(
                    access_token=access_token, index_id=position["index_id"], position_id=position["id"],
                    fyers_symbol=position["option_symbol"], lots=position["lots"], lot_size=position["lot_size"],
                    exit_price=last_known_price, exit_reason="strike_out_of_scope", pnl=pnl, pnl_pct=pnl_pct,
                    position_mode=position["mode"],
                )
                if not exit_result["executed"]:
                    # Real exit order REJECTED (or no token available) --
                    # position stays open in our records too, since it
                    # likely IS still open on Fyers. Already logged to
                    # rejection_log by trade_dispatcher/live_executor.
                    actions_taken.append({
                        "position_id": position["id"], "action": "exit_deferred", "reason": "strike_out_of_scope",
                        "note": "real exit order failed or no token available -- position remains open, will retry next cycle",
                    })
                    continue

                entry_ts = datetime.fromisoformat(position["entry_time"])
                hold_minutes = market_minutes_between(entry_ts, now)
                insert_trade_log(
                    position_id=position["id"], index_id=position["index_id"], direction=position["direction"],
                    option_symbol=position["option_symbol"], strike=position["strike"], lots=position["lots"],
                    entry_price=position["entry_price"], entry_time=position["entry_time"], exit_price=last_known_price,
                    exit_time=now.isoformat(), exit_reason="strike_out_of_scope", pnl=pnl, pnl_pct=pnl_pct,
                    hold_minutes=hold_minutes, mode=position["mode"],
                )
                actions_taken.append({
                    "position_id": position["id"], "action": "closed", "reason": "strike_out_of_scope",
                    "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                    "note": "closed using last known price -- strike no longer in tracked chain, no fresh LTP available",
                })
            continue  # can't evaluate further this cycle either way

        if get_index_risk_params_fn is not None:
            index_params = get_index_risk_params_fn(position["index_id"])
            pos_hard_sl_pct = index_params["hard_sl_pct"]
            pos_time_stop_minutes = index_params["time_stop_minutes"]
            pos_tsl_activation_pct = index_params["tsl_activation_pct"]
            pos_tsl_base_trail_pct = index_params["tsl_base_trail_pct"]
            pos_tsl_k = index_params["tsl_k"]
            pos_tsl_floor_pct = index_params["tsl_floor_pct"]
        else:
            pos_hard_sl_pct, pos_time_stop_minutes = hard_sl_pct, time_stop_minutes
            pos_tsl_activation_pct, pos_tsl_base_trail_pct = tsl_activation_pct, tsl_base_trail_pct
            pos_tsl_k, pos_tsl_floor_pct = tsl_k, tsl_floor_pct

        entry_price = position["entry_price"]
        current_profit_pct = (current_price - entry_price) / entry_price * 100
        peak_profit_pct = max(position["peak_profit_pct"] or 0.0, current_profit_pct)

        update_position_price(position["id"], current_price, peak_profit_pct)

        entry_ts = datetime.fromisoformat(position["entry_time"])
        now = datetime.now(IST)
        has_moved_favorably = peak_profit_pct > 0
        elapsed_market_min = market_minutes_between(entry_ts, now)

        # Two new, UNCONDITIONAL exit rules checked FIRST, ahead of SL/
        # TSL — both force an exit regardless of current profit/loss,
        # since they represent real operational constraints (can't hold
        # overnight; can't accurately monitor a position outside the
        # tracked chain range) rather than a profit/loss judgment call.
        session_end_check = check_session_end(now)
        strike_scope_check = check_strike_still_tracked(position["strike"], parsed_chain)

        hard_check = check_hard_stop_loss(
            entry_price=entry_price, current_price=current_price, entry_ts=entry_ts, now=now,
            premium_drop_pct=pos_hard_sl_pct, time_stop_minutes=pos_time_stop_minutes,
            has_moved_favorably=has_moved_favorably, elapsed_market_minutes=elapsed_market_min,
        )

        tsl_check = update_trailing_stop(
            peak_profit_pct=peak_profit_pct, current_profit_pct=current_profit_pct,
            activation_pct=pos_tsl_activation_pct, base_trail_pct=pos_tsl_base_trail_pct,
            k=pos_tsl_k, floor_pct=pos_tsl_floor_pct,
        )

        # Priority order: session-end and strike-scope are unconditional
        # overrides (checked first); then hard SL; then TSL. Whichever
        # triggers first in this order determines the exit reason.
        if session_end_check["triggered"]:
            should_close, exit_reason = True, session_end_check["reason"]
        elif strike_scope_check["triggered"]:
            should_close, exit_reason = True, strike_scope_check["reason"]
        else:
            should_close = hard_check["triggered"] or tsl_check["triggered"]
            exit_reason = hard_check["reason"] if hard_check["triggered"] else tsl_check["reason"]

        if should_close:
            pnl = (current_price - entry_price) * position["lots"] * position["lot_size"]
            pnl_pct = current_profit_pct

            exit_result = execute_exit(
                access_token=access_token, index_id=position["index_id"], position_id=position["id"],
                fyers_symbol=position["option_symbol"], lots=position["lots"], lot_size=position["lot_size"],
                exit_price=current_price, exit_reason=exit_reason, pnl=pnl, pnl_pct=pnl_pct,
                position_mode=position["mode"],
            )
            if not exit_result["executed"]:
                actions_taken.append({
                    "position_id": position["id"], "action": "exit_deferred", "reason": exit_reason,
                    "note": "real exit order failed or no token available -- position remains open, will retry next cycle",
                })
                continue

            hold_minutes = elapsed_market_min
            insert_trade_log(
                position_id=position["id"], index_id=position["index_id"], direction=position["direction"],
                option_symbol=position["option_symbol"], strike=position["strike"], lots=position["lots"],
                entry_price=entry_price, entry_time=position["entry_time"], exit_price=current_price,
                exit_time=now.isoformat(), exit_reason=exit_reason, pnl=pnl, pnl_pct=pnl_pct,
                hold_minutes=hold_minutes, mode=position["mode"],
            )

            actions_taken.append({
                "position_id": position["id"], "action": "closed", "reason": exit_reason,
                "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            })
        else:
            actions_taken.append({
                "position_id": position["id"], "action": "held",
                "current_profit_pct": round(current_profit_pct, 2), "peak_profit_pct": round(peak_profit_pct, 2),
            })

    return actions_taken