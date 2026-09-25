"""
Group B: IV Rank / Percentile (PRD §3.5-B). Avoid buying options when
current IV sits in a high historical percentile — even if VRP looks
favorable in isolation, buying at IV-rank extremes is a separate risk.

IV Rank = where current IV sits between the historical min and max (0-100).
IV Percentile = % of historical readings below the current IV (0-100).

Needs real history to be meaningful — with too little data we say so
explicitly rather than returning a misleading 50 (mid-range default).
"""
MIN_SAMPLES_FOR_IV_RANK = 20


def compute_iv_rank(current_iv_pct: float, historical_iv_pcts: list[float]) -> dict:
    if current_iv_pct is None:
        return {"iv_rank": None, "iv_percentile": None, "note": "no current IV available"}

    if len(historical_iv_pcts) < MIN_SAMPLES_FOR_IV_RANK:
        return {
            "iv_rank": None, "iv_percentile": None,
            "sample_size": len(historical_iv_pcts),
            "note": f"need at least {MIN_SAMPLES_FOR_IV_RANK} historical IV readings, have {len(historical_iv_pcts)} — history builds up over time as the app runs",
        }

    iv_min = min(historical_iv_pcts)
    iv_max = max(historical_iv_pcts)

    if iv_max == iv_min:
        iv_rank = 50.0  # no variation in history, can't rank meaningfully
    else:
        iv_rank = (current_iv_pct - iv_min) / (iv_max - iv_min) * 100
        iv_rank = max(0.0, min(100.0, iv_rank))  # clamp in case current is outside historical range

    below_count = sum(1 for v in historical_iv_pcts if v < current_iv_pct)
    iv_percentile = (below_count / len(historical_iv_pcts)) * 100

    return {
        "iv_rank": round(iv_rank, 1),
        "iv_percentile": round(iv_percentile, 1),
        "sample_size": len(historical_iv_pcts),
        "historical_min": round(iv_min, 2),
        "historical_max": round(iv_max, 2),
        "note": None,
    }
