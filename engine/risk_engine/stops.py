"""
Risk Engine — Stop Loss & Trailing Stop (PRD §4.8.1, §4.8.2). Hard exit
rules: whichever triggers first closes the position, no averaging down,
no discretion once live. The trailing stop uses a continuous-tightening
ratchet (distance shrinks as profit grows, floor never rises back up).
"""
from datetime import datetime, timedelta


def check_hard_stop_loss(entry_price: float, current_price: float, entry_ts: datetime,
                          now: datetime, premium_drop_pct: float, time_stop_minutes: int,
                          has_moved_favorably: bool, elapsed_market_minutes: float | None = None) -> dict:
    """
    elapsed_market_minutes: if provided, used directly for the time-stop
    check instead of naive (now - entry_ts) wall-clock minutes. Wall-
    clock minutes incorrectly count nights/weekends as elapsed trading
    time — a position opened Friday afternoon and checked Monday
    morning would show hundreds of wall-clock minutes elapsed even
    though only a few real trading minutes passed, triggering a
    premature/incorrect time-stop exit. Callers should compute this via
    engine.execution.paper_executor.market_minutes_between() and pass
    it in; falling back to wall-clock here only for backward
    compatibility with any caller that hasn't been updated yet.
    """
    if entry_price <= 0:
        return {"triggered": False, "reason": None, "note": "invalid entry price"}

    drop_pct = (entry_price - current_price) / entry_price * 100

    if drop_pct >= premium_drop_pct:
        return {"triggered": True, "reason": "hard_sl_hit", "drop_pct": round(drop_pct, 2)}

    if elapsed_market_minutes is not None:
        elapsed_minutes = elapsed_market_minutes
    else:
        elapsed_minutes = (now - entry_ts).total_seconds() / 60

    if elapsed_minutes >= time_stop_minutes and not has_moved_favorably:
        return {"triggered": True, "reason": "time_stop", "elapsed_minutes": round(elapsed_minutes, 1)}

    return {"triggered": False, "reason": None, "drop_pct": round(drop_pct, 2)}


def check_session_end(now: "datetime", market_close_hour: int = 15, market_close_minute: int = 30,
                       buffer_minutes: int = 2) -> dict:
    """
    New rule: no overnight position holding. Any position still open
    within `buffer_minutes` of market close (default 2 min, closing at
    15:28 rather than exactly 15:30 to leave room for the exit order
    itself to actually execute before the exchange stops accepting
    orders) is force-closed, regardless of profit or loss.

    Previously there was NO such check anywhere in the app — real
    trades in the trade log confirm positions were genuinely being
    held overnight (e.g. an entry on one day closing the next morning
    via a normal SL/TSL/time-stop hit, not a deliberate session-end
    exit). This closes that real gap.
    """
    close_boundary = now.replace(hour=market_close_hour, minute=market_close_minute, second=0, microsecond=0)
    trigger_time = close_boundary - timedelta(minutes=buffer_minutes)

    if now >= trigger_time:
        return {"triggered": True, "reason": "session_end_exit",
                "note": f"forced exit — within {buffer_minutes} min of {market_close_hour:02d}:{market_close_minute:02d} session close"}
    return {"triggered": False, "reason": None}




def compute_trail_distance(peak_profit_pct: float, activation_pct: float,
                            base_trail_pct: float, k: float, floor_pct: float) -> float:
    if peak_profit_pct < activation_pct:
        return base_trail_pct  # TSL not active yet
    profit_above_activation = peak_profit_pct - activation_pct
    return max(floor_pct, base_trail_pct - k * profit_above_activation)


def update_trailing_stop(peak_profit_pct: float, current_profit_pct: float,
                          activation_pct: float = 15.0, base_trail_pct: float = 12.0,
                          k: float = 0.15, floor_pct: float = 5.0) -> dict:
    active = peak_profit_pct >= activation_pct
    trail_distance = compute_trail_distance(peak_profit_pct, activation_pct, base_trail_pct, k, floor_pct)
    locked_in_floor = max(0.0, peak_profit_pct - trail_distance) if active else 0.0
    triggered = active and current_profit_pct <= locked_in_floor

    return {
        "active": active,
        "peak_profit_pct": round(peak_profit_pct, 2),
        "current_profit_pct": round(current_profit_pct, 2),
        "trail_distance_pct": round(trail_distance, 2),
        "locked_in_floor_pct": round(locked_in_floor, 2),
        "triggered": triggered,
        "reason": "tsl_hit" if triggered else None,
    }
