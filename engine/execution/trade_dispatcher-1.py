"""
Trade Execution Dispatcher — the single real decision point for
whether an entry/exit becomes a PAPER trade (simulated, no real money)
or a LIVE trade (real order via engine/execution/live_executor.py).

TWO INDEPENDENT SAFETY GATES must both agree before ANY real order is
placed, checked fresh on every single call — never cached, never
assumed from a previous check:

  1. The account-level master switch (account_config.live_trading_master_enabled)
  2. This specific index's own mode setting (index_config.mode == "live" or "both")

If EITHER gate is off, the trade always falls through to paper
execution — simulated, no real capital at risk, matching this app's
entire history of defaulting to the safe path. This mirrors real
industrial safety-interlock design (two independent switches, not one)
deliberately, given the seriousness of real money being placed.
"""
from config.trading_config import get_account_config, get_index_config
from config.db import open_position, close_position, log_trade_action
from engine.execution.live_executor import place_live_entry_order, place_live_exit_order


def is_live_trading_active_for(index_id: str) -> bool:
    """
    The real, single source of truth for "will this index's next trade
    use real money". Both gates are read FRESH here, every call — a
    person turning live trading off mid-session must take effect on
    the very next entry/exit decision, not after some stale cached
    check.
    """
    account = get_account_config()
    if not account.get("live_trading_master_enabled", False):
        return False
    index_config = get_index_config(index_id)
    if index_config is None:
        return False
    return index_config.get("mode") in ("live", "both")


def execute_entry(access_token: str, index_id: str, fyers_symbol: str, direction: str,
                   strike: float, option_type: str, lots: int, lot_size: int,
                   simulated_or_live_price: float) -> dict:
    """
    The single real entry point every part of this app should call to
    open a position — decides paper vs. live internally, so callers
    (the auto-entry loop, manual entry endpoints, etc.) never need to
    duplicate this safety-gate logic themselves.

    simulated_or_live_price: for paper trades, this is the simulated
    entry price (e.g. current option chain LTP). For live trades, this
    is ALSO the current LTP at decision time — used to record the
    position immediately on order acceptance, since Fyers' place_order
    response confirms ACCEPTANCE, not a guaranteed fill price; the live
    LTP at submission time is the standard, honest approximation used
    elsewhere in this app for the same reason.
    """
    if is_live_trading_active_for(index_id):
        qty = lots * lot_size
        result = place_live_entry_order(
            access_token, index_id, fyers_symbol, qty, direction, strike, option_type, lots, lot_size,
        )
        if result["status"] != "accepted":
            # Rejected or errored — already logged by live_executor
            # itself (rejection_log). NO position is opened; a
            # rejected order was never a real trade.
            return {"executed": False, "mode": "live", "result": result}

        position_id = open_position(
            index_id, direction, fyers_symbol, strike, option_type, lots, lot_size,
            simulated_or_live_price, mode="live",
        )
        log_trade_action(position_id, index_id, "live_order_accepted", f"fyers_order_id={result['fyers_order_id']}")
        return {"executed": True, "mode": "live", "position_id": position_id, "result": result}

    else:
        position_id = open_position(
            index_id, direction, fyers_symbol, strike, option_type, lots, lot_size,
            simulated_or_live_price, mode="paper",
        )
        return {"executed": True, "mode": "paper", "position_id": position_id}


def execute_exit(access_token: str, index_id: str, position_id: int, fyers_symbol: str,
                  lots: int, lot_size: int, exit_price: float, exit_reason: str,
                  pnl: float, pnl_pct: float, position_mode: str) -> dict:
    """
    The single real exit point. position_mode is the MODE THE POSITION
    WAS OPENED UNDER (stored on the position itself) — NOT re-evaluated
    against current live settings. This is deliberate: a position
    opened as a real live trade must be closed with a real live sell
    order regardless of whether someone toggled live trading off
    partway through that trade's life — otherwise a real open position
    could be silently abandoned with no real closing order ever sent.
    """
    if position_mode == "live":
        qty = lots * lot_size
        result = place_live_exit_order(access_token, index_id, fyers_symbol, qty, position_id, exit_reason)
        if result["status"] != "accepted":
            # Exit rejected — the position is NOT marked closed in our
            # records, since it likely is NOT actually closed on
            # Fyers' side either. Already logged (both to this
            # position's own action history AND the global
            # rejection_log) by live_executor.
            return {"executed": False, "mode": "live", "result": result}

        close_position(position_id, exit_price, exit_reason, pnl, pnl_pct)
        return {"executed": True, "mode": "live", "result": result}

    else:
        close_position(position_id, exit_price, exit_reason, pnl, pnl_pct)
        return {"executed": True, "mode": "paper"}
