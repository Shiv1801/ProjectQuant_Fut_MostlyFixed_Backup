"""
Risk Engine — Circuit Breakers (PRD §4.8.4). Portfolio & account-level
safety switches, independent of any single trade's own SL/TSL.
"""


def evaluate_circuit_breakers(
    daily_pnl_pct: float,
    consecutive_losses: int,
    open_position_count: int,
    daily_loss_limit_pct: float = 5.0,
    consecutive_loss_throttle: int = 3,
    consecutive_loss_size_multiplier: float = 0.5,
    max_concurrent_positions: int = 3,
) -> dict:
    breaker_tripped = False
    breaker_reason = None
    size_multiplier = 1.0

    if daily_pnl_pct <= -daily_loss_limit_pct:
        breaker_tripped = True
        breaker_reason = "daily_loss_limit"

    if consecutive_losses >= consecutive_loss_throttle:
        size_multiplier = consecutive_loss_size_multiplier

    if open_position_count >= max_concurrent_positions and not breaker_tripped:
        breaker_tripped = True
        breaker_reason = "max_concurrent_positions"

    return {
        "breaker_tripped": breaker_tripped,
        "breaker_reason": breaker_reason,
        "size_multiplier": size_multiplier,
        "daily_pnl_pct": daily_pnl_pct,
        "consecutive_losses": consecutive_losses,
        "open_position_count": open_position_count,
    }


def check_weekly_drawdown(current_equity: float, peak_equity: float, cap_pct: float = 15.0) -> dict:
    if peak_equity <= 0:
        return {"breach": False, "drawdown_pct": 0.0, "note": "invalid peak equity"}

    drawdown_pct = (peak_equity - current_equity) / peak_equity * 100
    breach = drawdown_pct >= cap_pct

    return {
        "breach": breach,
        "drawdown_pct": round(drawdown_pct, 2),
        "cap_pct": cap_pct,
        "note": "weekly drawdown cap breached — should auto-downgrade to paper mode" if breach else None,
    }
