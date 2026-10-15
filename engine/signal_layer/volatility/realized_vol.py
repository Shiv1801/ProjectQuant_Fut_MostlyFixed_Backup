
"""
Group A: Realized Volatility from stored one-minute candle closes.

Output unit: annualized volatility in percentage points.
Assumes consecutive input prices represent consecutive one-minute
observations during regular trading sessions.
"""

import math

TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375
ANNUALIZATION_FACTOR = math.sqrt(
    TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION
)


def compute_realized_vol(closes: list[float]) -> dict:
    if closes is None or len(closes) < 3:
        return {
            "realized_vol_pct": None,
            "sample_size": 0 if closes is None else max(0, len(closes) - 1),
            "note": "need at least 3 candle closes",
        }

    try:
        prices = [float(price) for price in closes]
    except (TypeError, ValueError, OverflowError):
        return {
            "realized_vol_pct": None,
            "sample_size": 0,
            "note": "invalid price data: non-numeric value",
        }

    if not all(math.isfinite(p) and p > 0 for p in prices):
        return {
            "realized_vol_pct": None,
            "sample_size": 0,
            "note": "invalid price data: prices must be finite and positive",
        }

    # Log differences avoid unnecessary division before taking logarithms.
    log_returns = [
        math.log(prices[i]) - math.log(prices[i - 1])
        for i in range(1, len(prices))
    ]

    n = len(log_returns)
    mean_return = math.fsum(log_returns) / n

    variance = math.fsum(
        (r - mean_return) ** 2 for r in log_returns
    ) / (n - 1)

    vol_pct = math.sqrt(variance) * ANNUALIZATION_FACTOR * 100

    if not math.isfinite(vol_pct):
        return {
            "realized_vol_pct": None,
            "sample_size": n,
            "note": "non-finite volatility estimate",
        }

    return {
        "realized_vol_pct": round(vol_pct, 4),
        "sample_size": n,
        "window_minutes": len(prices),
        "note": None,
    }
