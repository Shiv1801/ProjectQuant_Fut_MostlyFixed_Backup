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
        return {
            "full_kelly_fraction": None,
            "sized_fraction": None,
            "trade_count": trade_count,
            "note": "missing win rate or avg win/loss stats",
        }

    if avg_loss_pct <= 0:
        return {
            "full_kelly_fraction": None,
            "sized_fraction": None,
            "trade_count": trade_count,
            "note": "avg_loss_pct must be positive (magnitude of average loss)",
        }

    b = avg_win_pct / avg_loss_pct  # payoff ratio
    full_kelly = win_rate - (1 - win_rate) / b

    # Kelly is a sizing modifier, not an entry veto.
    # Negative/zero Kelly means there is no demonstrated Kelly edge,
    # but it must NOT automatically prevent an otherwise-valid setup
    # from trading. Neutral sizing is used instead.
    if full_kelly <= 0:
        sized_fraction = 0.25
        note = (
            "negative/zero Kelly edge detected — "
            "using neutral fractional-Kelly sizing; "
            "entry gates remain authoritative"
        )
    else:
        sized_fraction = full_kelly * kelly_fraction
        note = None

    return {
        "full_kelly_fraction": round(full_kelly, 4),
        "sized_fraction": round(sized_fraction, 4),
        "kelly_fraction_used": kelly_fraction,
        "payoff_ratio": round(b, 3),
        "trade_count": trade_count,
        "note": note,
    }