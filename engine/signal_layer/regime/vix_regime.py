"""
Group E: VIX Level + Rate of Change Regime Detector.

Input is a list of oldest-first one-minute VIX candles with a `close` field.
The model labels a volatility regime; it does not itself change position sizing.
Any sizing response must be evaluated separately in backtests.
"""
import math
from numbers import Real


def _finite_positive(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def compute_vix_regime(
    vix_candles: list[dict],
    elevated_threshold: float = 18.0,
    roc_threshold_pct: float = 5.0,
) -> dict:
    """Return VIX level, five-minute ROC, regime, and an explanatory note.

    The 5-minute ROC compares the latest close to the close five one-minute
    intervals earlier, which requires six one-minute closes.
    """
    threshold = _finite_positive(elevated_threshold)
    roc_threshold = _finite_positive(roc_threshold_pct)
    if threshold is None or roc_threshold is None:
        return {
            "vix": None, "roc_5min_pct": None, "elevated": None,
            "rising_fast": None, "regime": None, "interpretation": None,
            "note": "thresholds must be finite positive numbers",
        }

    if not isinstance(vix_candles, list) or not vix_candles:
        return {
            "vix": None, "roc_5min_pct": None, "elevated": None,
            "rising_fast": None, "regime": None, "interpretation": None,
            "note": "no VIX candle data available",
        }

    closes = []
    for index, candle in enumerate(vix_candles):
        if not isinstance(candle, dict) or "close" not in candle:
            return {
                "vix": None, "roc_5min_pct": None, "elevated": None,
                "rising_fast": None, "regime": None, "interpretation": None,
                "note": f"invalid VIX candle at index {index}: missing close",
            }
        close = _finite_positive(candle.get("close"))
        if close is None:
            return {
                "vix": None, "roc_5min_pct": None, "elevated": None,
                "rising_fast": None, "regime": None, "interpretation": None,
                "note": f"invalid VIX close at index {index}: must be finite and positive",
            }
        closes.append(close)

    current_vix = closes[-1]
    elevated = current_vix > threshold

    # Keep regime=None when history is insufficient, so a caller cannot
    # accidentally interpret an incomplete reading as a fully classified regime.
    if len(closes) < 6:
        return {
            "vix": round(current_vix, 2),
            "roc_5min_pct": None,
            "elevated": elevated,
            "rising_fast": None,
            "regime": None,
            "interpretation": "VIX level available; five-minute ROC is unavailable",
            "note": "need at least 6 one-minute candles for 5-min ROC",
        }

    vix_5min_ago = closes[-6]
    roc_pct = (current_vix - vix_5min_ago) / vix_5min_ago * 100.0
    rising_fast = roc_pct > roc_threshold

    if elevated and rising_fast:
        regime = "panic_regime"
        interpretation = "VIX elevated and rising rapidly; review risk controls"
    elif elevated:
        regime = "elevated_vol"
        interpretation = "VIX elevated but not rising above the rapid-rise threshold"
    elif rising_fast:
        regime = "vol_expansion_starting"
        interpretation = "VIX rising rapidly from below the elevated threshold"
    else:
        regime = "calm"
        interpretation = "VIX below the elevated threshold and not rising rapidly"

    return {
        "vix": round(current_vix, 2),
        "roc_5min_pct": round(roc_pct, 2),
        "elevated": elevated,
        "rising_fast": rising_fast,
        "regime": regime,
        "interpretation": interpretation,
        "note": None,
    }
