"""
Group E: Realized / Implied Volatility Ratio.

Compares Group A annualized realized volatility with annualized implied
volatility. Both inputs must use percentage points (e.g. 12.5 means 12.5%).
The ratio is dimensionless and is a regime/relative-pricing diagnostic, not
a standalone directional signal.

The 0.8 and 1.2 interpretation thresholds are preserved from the prior
implementation; they should be validated out of sample before being used
as trade-entry or position-sizing rules.
"""
import math
from numbers import Real


def _finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        try:
            # Accept numeric strings for compatibility with data-layer values.
            if isinstance(value, str):
                value = float(value)
            else:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def compute_rv_iv_ratio(
    realized_vol_pct: float | None,
    implied_vol_pct: float | None,
) -> dict:
    """
    Args:
        realized_vol_pct: annualized Group A realized volatility in percent.
        implied_vol_pct: annualized implied volatility in percent, on the
            same annualization basis as realized_vol_pct.

    Returns the existing result keys on every path to reduce caller breakage.
    """
    realized = _finite_number(realized_vol_pct)
    implied = _finite_number(implied_vol_pct)

    result = {
        "rv_iv_ratio": None,
        "realized_vol_pct": realized,
        "implied_vol_pct": implied,
        "interpretation": None,
        "note": None,
    }

    if realized_vol_pct is None or implied_vol_pct is None:
        result["note"] = "missing realized or implied vol"
        return result

    if realized is None or implied is None:
        result["note"] = "realized and implied volatility must be finite numeric values"
        return result

    if realized < 0:
        result["note"] = "realized volatility cannot be negative"
        return result

    if implied <= 0:
        result["note"] = "implied volatility must be positive"
        return result

    ratio = realized / implied
    if not math.isfinite(ratio):
        result["note"] = "ratio is non-finite"
        return result

    result["rv_iv_ratio"] = round(ratio, 3)
    result["realized_vol_pct"] = round(realized, 2)
    result["implied_vol_pct"] = round(implied, 2)

    if ratio >= 1.2:
        result["interpretation"] = (
            "realized vol running hotter than implied — options may be underpriced"
        )
    elif ratio < 0.8:
        result["interpretation"] = (
            "realized vol running cooler than implied — options may be overpriced"
        )
    else:
        result["interpretation"] = "realized and implied vol roughly aligned"

    return result
