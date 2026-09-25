"""
PRD §4.8.1 — Initial Stop-Loss: premium-based hard SL + time-based theta stop.
Hard exit rule: whichever triggers first closes the position. No averaging
down, no discretion once live.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class StopLossCheck:
    triggered: bool
    reason: str | None  # "hard_sl_hit" | "time_stop" | None


def check_initial_stop_loss(entry_price: float, current_price: float, entry_ts: datetime,
                             now: datetime, premium_drop_pct: float, time_stop_minutes: int,
                             has_moved_favorably: bool) -> StopLossCheck:
    drop_pct = (entry_price - current_price) / entry_price * 100
    if drop_pct >= premium_drop_pct:
        return StopLossCheck(True, "hard_sl_hit")

    elapsed = now - entry_ts
    if elapsed >= timedelta(minutes=time_stop_minutes) and not has_moved_favorably:
        return StopLossCheck(True, "time_stop")

    return StopLossCheck(False, None)
