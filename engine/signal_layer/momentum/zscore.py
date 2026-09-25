"""
Group C: Z-Score of price vs rolling mean (PRD §3.5-C). Flags how many
standard deviations the current price sits from its recent rolling
average — used for mean-reversion entries when Hurst indicates a
mean-reverting regime, and as a general "how stretched is price right
now" signal.
"""
import math

MIN_CANDLES_FOR_ZSCORE = 20


def compute_zscore(closes: list[float], window: int = 20) -> dict:
    if len(closes) < MIN_CANDLES_FOR_ZSCORE:
        return {
            "zscore": None, "sample_size": len(closes),
            "note": f"need at least {MIN_CANDLES_FOR_ZSCORE} candles, have {len(closes)}",
        }

    recent = closes[-window:]
    mean = sum(recent) / len(recent)
    variance = sum((c - mean) ** 2 for c in recent) / (len(recent) - 1)
    std = math.sqrt(variance)

    if std == 0:
        return {"zscore": 0.0, "mean": round(mean, 2), "std": 0.0, "sample_size": len(recent), "note": "flat price series, zscore=0"}

    current_price = closes[-1]
    z = (current_price - mean) / std

    if z > 2:
        interpretation = "extended_above (2+ std) — potential mean-reversion candidate"
    elif z < -2:
        interpretation = "extended_below (2+ std) — potential mean-reversion candidate"
    elif abs(z) < 0.5:
        interpretation = "near mean — no significant deviation"
    else:
        interpretation = "moderate deviation"

    return {
        "zscore": round(z, 2),
        "mean": round(mean, 2),
        "std": round(std, 2),
        "current_price": current_price,
        "sample_size": len(recent),
        "interpretation": interpretation,
        "note": None,
    }
