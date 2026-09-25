"""
Group E: Realized/Implied Vol Ratio — PRD §3.5-E. Secondary regime
confirmation; cross-checks Group A (realized vol) against Group B
(implied vol / VRP) from a different angle — a ratio far from 1 flags
where the options market's pricing has diverged from actual recent
price behavior.
"""


def compute_rv_iv_ratio(realized_vol_pct: float | None, implied_vol_pct: float | None) -> dict:
    if realized_vol_pct is None or implied_vol_pct is None:
        return {"rv_iv_ratio": None, "note": "missing realized or implied vol"}

    if implied_vol_pct == 0:
        return {"rv_iv_ratio": None, "note": "implied vol is zero, cannot compute ratio"}

    ratio = realized_vol_pct / implied_vol_pct

    if ratio >= 1.2:
        interpretation = "realized vol running hotter than implied — options may be underpriced"
    elif ratio < 0.8:
        interpretation = "realized vol running cooler than implied — options may be overpriced"
    else:
        interpretation = "realized and implied vol roughly aligned"

    return {
        "rv_iv_ratio": round(ratio, 3),
        "realized_vol_pct": round(realized_vol_pct, 2),
        "implied_vol_pct": round(implied_vol_pct, 2),
        "interpretation": interpretation,
        "note": None,
    }
