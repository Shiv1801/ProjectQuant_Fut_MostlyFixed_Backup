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
import threading
from config.db import open_position, close_position, log_trade_action, get_position_by_id
from engine.execution.live_executor import place_live_entry_order, place_live_exit_order
from engine.confluence_gate.direction import validate_entry_candidate

# Per-position exit serialization. This protects concurrent manual/automatic
# callers in this process without serializing exits for unrelated positions.
_exit_locks_guard = threading.Lock()
_exit_locks: dict[int, threading.Lock] = {}
_exit_uncertain: set[int] = set()

def _exit_lock_for(position_id: int) -> threading.Lock:
    with _exit_locks_guard:
        lock = _exit_locks.get(position_id)
        if lock is None:
            lock = threading.Lock()
            _exit_locks[position_id] = lock
        return lock


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
                    simulated_or_live_price: float, quant_confidence: float | None = None,
                    authorization: dict | None = None, intent: str = "NEW_ENTRY") -> dict:
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
    if intent != "NEW_ENTRY":
        return {
            "executed": False, "mode": "rejected",
            "result": {"status": "rejected", "message": "entry dispatcher accepts NEW_ENTRY intent only"},
        }

    # Entry convention: bullish/long-call signals BUY CE; bearish/long-put
    # signals BUY PE. A bearish signal is NOT a broker SELL order. Exits
    # remain separate and use SELL only to close an already-bought option.
    expected_option_by_direction = {
        "long_call": "CE",
        "bullish": "CE",
        "buy": "CE",
        "long_put": "PE",
        "bearish": "PE",
        "sell": "PE",
    }
    expected_option_type = expected_option_by_direction.get(str(direction).strip().lower())
    normalized_option_type = str(option_type).strip().upper()
    if expected_option_type is None:
        return {
            "executed": False, "mode": "rejected",
            "result": {"status": "rejected", "message": f"unsupported entry direction: {direction}"},
        }
    if normalized_option_type != expected_option_type:
        reason = (
            f"direction/option mismatch: direction={direction} requires BUY {expected_option_type}, "
            f"but selected option_type={option_type}"
        )
        log_trade_action(0, index_id, "entry_rejected_direction_option_mismatch", reason)
        return {
            "executed": False,
            "mode": "rejected",
            "result": {
                "status": "rejected",
                "message": reason,
                "directional_validation": {
                    "valid": False,
                    "reason": "option_type_conflicts_with_quant_direction",
                    "expected_option_type": expected_option_type,
                    "actual_option_type": normalized_option_type,
                },
            },
        }
    # Reject a mismatched contract symbol instead of silently buying a
    # different instrument than the selector supplied.
    if not str(fyers_symbol).strip().upper().endswith(expected_option_type):
        reason = f"contract symbol does not match required {expected_option_type}: {fyers_symbol}"
        log_trade_action(0, index_id, "entry_rejected_symbol_option_mismatch", reason)
        return {"executed": False, "mode": "rejected", "result": {"status": "rejected", "message": reason}}

    directional_validation = validate_entry_candidate(direction, normalized_option_type, fyers_symbol, index_id)
    if not directional_validation["valid"]:
        return {
            "executed": False,
            "mode": "rejected",
            "result": {
                "status": "rejected",
                "message": directional_validation["reason"],
                "directional_validation": directional_validation,
            },
        }

    if not isinstance(authorization, dict) or authorization.get("fired") is not True:
        return {"executed": False, "mode": "rejected", "result": {"status": "rejected", "message": "authoritative confluence authorization required"}}
    if authorization.get("direction") != direction:
        return {"executed": False, "mode": "rejected", "result": {"status": "rejected", "message": "authorization direction mismatch"}}

    if is_live_trading_active_for(index_id):
        qty = lots * lot_size
        result = place_live_entry_order(
            access_token, index_id, fyers_symbol, qty, direction, strike, normalized_option_type, lots, lot_size,
            authorization=authorization,
        )
        if result["status"] != "accepted":
            # Rejected or errored — already logged by live_executor
            # itself (rejection_log). NO position is opened; a
            # rejected order was never a real trade.
            return {"executed": False, "mode": "live", "result": result}

        position_id = open_position(
            index_id, direction, fyers_symbol, strike, normalized_option_type, lots, lot_size,
            simulated_or_live_price, mode="live",
        )
        log_trade_action(position_id, index_id, "live_order_accepted", f"fyers_order_id={result['fyers_order_id']}")
        return {"executed": True, "mode": "live", "position_id": position_id, "result": result}

    else:
        position_id = open_position(
            index_id, direction, fyers_symbol, strike, normalized_option_type, lots, lot_size,
            simulated_or_live_price, mode="paper",
        )
        return {"executed": True, "mode": "paper", "position_id": position_id}


def execute_exit(access_token: str, index_id: str, position_id: int, fyers_symbol: str,
                  lots: int, lot_size: int, exit_price: float, exit_reason: str,
                  pnl: float, pnl_pct: float, position_mode: str) -> dict:
    """Serialize exits per position to prevent concurrent duplicate SELLs.

    A broker exception is an ambiguous outcome: Fyers may have accepted the
    order even if the response was lost. Such positions are blocked from
    automatic resubmission in this process until the order is reconciled.
    """
    with _exit_locks_guard:
        if position_id in _exit_uncertain:
            log_trade_action(position_id, index_id, "exit_blocked_uncertain",
                             "Previous exit outcome is unknown; reconcile broker order before retrying")
            return {"executed": False, "mode": position_mode,
                    "result": {"status": "uncertain", "message": "previous exit outcome unknown; reconcile Fyers order book before retry"}}
        lock = _exit_locks.get(position_id)
        if lock is None:
            lock = threading.Lock()
            _exit_locks[position_id] = lock

    if not lock.acquire(blocking=False):
        log_trade_action(position_id, index_id, "exit_blocked_in_progress",
                         "Another exit request is already processing for this position")
        return {"executed": False, "mode": position_mode,
                "result": {"status": "in_progress", "message": "exit already processing for this position"}}

    keep_blocked = False
    try:
        position = get_position_by_id(position_id)
        requested_lots = lots
        if (
            not position
            or position.get("status") != "open"
            or position.get("index_id") != index_id
            or position.get("option_symbol") != fyers_symbol
            or not isinstance(requested_lots, int)
            or requested_lots < 1
            or requested_lots > position.get("lots", 0)
            or position_mode != position.get("mode")
        ):
            reason = "invalid_exit_position_or_quantity"
            log_trade_action(position_id, index_id, "exit_rejected", reason)
            return {"executed": False, "mode": position_mode,
                    "result": {"status": "rejected", "message": reason}}

        if position_mode == "live":
            qty = lots * lot_size
            result = place_live_exit_order(access_token, index_id, fyers_symbol, qty, position_id, exit_reason)
            if result.get("status") == "error" or result.get("status") == "uncertain":
                # Fail closed. Do not allow the 0.5-second monitor or a manual
                # click to blindly submit another SELL after an ambiguous response.
                keep_blocked = True
                with _exit_locks_guard:
                    _exit_uncertain.add(position_id)
                log_trade_action(position_id, index_id, "exit_outcome_uncertain",
                                 "Fyers response ambiguous; reconcile order book before any retry")
                return {"executed": False, "mode": "live", "result": result}
            if result.get("status") != "accepted":
                # A definite broker rejection can be retried after the cause is resolved.
                return {"executed": False, "mode": "live", "result": result}

            try:
                close_position(position_id, exit_price, exit_reason, pnl, pnl_pct)
            except Exception as exc:
                # Order accepted but local close failed: do not submit another SELL.
                keep_blocked = True
                with _exit_locks_guard:
                    _exit_uncertain.add(position_id)
                log_trade_action(position_id, index_id, "exit_close_record_failed",
                                 f"Fyers accepted order_id={result.get('fyers_order_id')}; local close failed: {exc}")
                return {"executed": False, "mode": "live", "result": {
                    "status": "uncertain", "message": "Fyers accepted exit but local position update failed; reconcile broker order before retry",
                    "fyers_order_id": result.get("fyers_order_id")}}
            return {"executed": True, "mode": "live", "result": result}

        close_position(position_id, exit_price, exit_reason, pnl, pnl_pct)
        return {"executed": True, "mode": "paper"}
    finally:
        # Keep a permanent in-process block only when the broker/local state is
        # ambiguous. All other paths release the per-position lock normally.
        if not keep_blocked:
            lock.release()
