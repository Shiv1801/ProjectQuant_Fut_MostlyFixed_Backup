"""
Group F: Fractional Kelly Criterion — PRD §3.5-F, §4.5. Core lever for
compounding target without over-risking. Sized down (e.g. quarter-Kelly)
from the theoretical full-Kelly fraction, which is known to be too
aggressive for real-world use given estimation error in win rate/payoff.

Needs live win_rate / avg_win / avg_loss stats to be meaningful — until
enough trades exist, this returns a clear "insufficient trade history"
note rather than a misleading number based on nothing.
"""
MIN_TRADES_FOR_KELLY = 30


def compute_fractional_kelly(win_rate: float | None, avg_win_pct: float | None,
                              avg_loss_pct: float | None, trade_count: int = 0,
                              kelly_fraction: float = 0.25) -> dict:
    if trade_count < MIN_TRADES_FOR_KELLY:
        return {
            "full_kelly_fraction": None, "sized_fraction": None, "trade_count": trade_count,
            "note": f"need at least {MIN_TRADES_FOR_KELLY} closed trades for a meaningful Kelly estimate, have {trade_count}",
        }

    if win_rate is None or avg_win_pct is None or avg_loss_pct is None:
        return {"full_kelly_fraction": None, "sized_fraction": None, "trade_count": trade_count, "note": "missing win rate or avg win/loss stats"}

    if avg_loss_pct <= 0:
        return {"full_kelly_fraction": None, "sized_fraction": None, "trade_count": trade_count, "note": "avg_loss_pct must be positive (magnitude of average loss)"}

    b = avg_win_pct / avg_loss_pct  # payoff ratio
    full_kelly = win_rate - (1 - win_rate) / b

    sized_fraction = max(0.0, full_kelly * kelly_fraction)

    if full_kelly <= 0:
        note = "negative edge detected (full Kelly <= 0) — sizing floored at 0, do not trade this setup"
    else:
        note = None

    return {
        "full_kelly_fraction": round(full_kelly, 4),
        "sized_fraction": round(sized_fraction, 4),
        "kelly_fraction_used": kelly_fraction,
        "payoff_ratio": round(b, 3),
        "trade_count": trade_count,
        "note": note,
    }
