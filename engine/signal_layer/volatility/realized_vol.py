"""
Group A: Realized Volatility, computed from stored 1-min candle closes.
This is the first real quant model in the A-G stack (PRD §3.5-A).
Formula: annualized stdev of log returns over the trailing window.
"""
import math


def compute_realized_vol(closes: list[float]) -> dict:
    """
    closes: list of close prices, oldest first, at least 2 needed.
    Returns annualized volatility (%) using log returns, scaled by
    sqrt(252 trading days * 375 one-min bars/day).
    """
    if len(closes) < 2:
        return {"realized_vol_pct": None, "sample_size": len(closes), "note": "need at least 2 candles"}

    log_returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        log_returns.append(math.log(closes[i] / closes[i - 1]))

    if len(log_returns) < 2:
        return {"realized_vol_pct": None, "sample_size": len(log_returns), "note": "not enough valid returns"}

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    stdev = math.sqrt(variance)

    annualized_vol = stdev * math.sqrt(252 * 375) * 100  # as percentage

    return {
        "realized_vol_pct": round(annualized_vol, 2),
        "sample_size": len(log_returns),
        "window_minutes": len(closes),
    }
