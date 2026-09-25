"""
Group D: Put-Call Ratio (OI-weighted) — PRD §3.5-D. Sentiment/positioning
extreme detector; contrarian signal input (very high PCR = excess
bearishness, often contrarian bullish; very low PCR = excess bullishness).
"""


def compute_pcr(call_oi_total: int | None, put_oi_total: int | None) -> dict:
    if call_oi_total is None or put_oi_total is None:
        return {"pcr": None, "note": "missing OI totals"}

    if call_oi_total == 0:
        return {"pcr": None, "note": "call OI is zero, cannot compute ratio"}

    pcr = put_oi_total / call_oi_total

    if pcr > 1.3:
        interpretation = "elevated PCR — excess bearish positioning, potential contrarian bullish signal"
    elif pcr < 0.7:
        interpretation = "low PCR — excess bullish positioning, potential contrarian bearish signal"
    else:
        interpretation = "neutral positioning"

    return {
        "pcr": round(pcr, 3),
        "call_oi_total": call_oi_total,
        "put_oi_total": put_oi_total,
        "interpretation": interpretation,
        "note": None,
    }
