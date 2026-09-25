"""
Group E: VIX Level + Rate of Change — PRD §3.5-E. Master regime switch;
changes position sizing system-wide. Elevated + rapidly rising VIX
signals a regime shift toward panic/high-vol, independent of what any
single index's price action shows.
"""


def compute_vix_regime(vix_candles: list[dict], elevated_threshold: float = 18.0,
                        roc_threshold_pct: float = 5.0) -> dict:
    """
    vix_candles: list of candle dicts (oldest first) with a 'close' field,
                 same shape as our stored candles table.
    """
    if not vix_candles:
        return {"vix": None, "roc_5min_pct": None, "note": "no VIX candle data available"}

    current_vix = vix_candles[-1]["close"]

    if len(vix_candles) < 6:
        return {
            "vix": current_vix, "roc_5min_pct": None, "elevated": current_vix > elevated_threshold,
            "note": "need at least 6 candles for 5-min ROC, showing level only",
        }

    vix_5min_ago = vix_candles[-6]["close"]
    if vix_5min_ago == 0:
        roc_pct = None
    else:
        roc_pct = (current_vix - vix_5min_ago) / vix_5min_ago * 100

    elevated = current_vix > elevated_threshold
    rising_fast = roc_pct is not None and roc_pct > roc_threshold_pct

    if elevated and rising_fast:
        regime = "panic_regime"
        interpretation = "VIX elevated and rising fast — high-vol/panic regime, reduce size and widen stops"
    elif elevated:
        regime = "elevated_vol"
        interpretation = "VIX elevated but stable — cautious regime"
    elif rising_fast:
        regime = "vol_expansion_starting"
        interpretation = "VIX rising fast from low base — possible regime shift beginning"
    else:
        regime = "calm"
        interpretation = "VIX calm and stable — normal regime"

    return {
        "vix": round(current_vix, 2),
        "roc_5min_pct": round(roc_pct, 2) if roc_pct is not None else None,
        "elevated": elevated,
        "rising_fast": rising_fast,
        "regime": regime,
        "interpretation": interpretation,
        "note": None,
    }
