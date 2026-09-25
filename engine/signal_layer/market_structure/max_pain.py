"""
Group D: Max Pain — PRD §3.5-D. The strike where aggregate option writer
payout is minimized (i.e. where most options expire worthless). Used as
an expiry-day pinning-risk signal — price has a statistical tendency to
gravitate toward max pain near expiry, though this is a soft tendency,
not a guarantee.
"""


def compute_max_pain(chain_rows: list[dict]) -> dict:
    """
    chain_rows: list of {"strike": float, "CE": {"oi":...}, "PE": {"oi":...}}
    """
    strikes = sorted({row["strike"] for row in chain_rows if row.get("strike") is not None})
    if not strikes:
        return {"max_pain_strike": None, "note": "no strikes in chain"}

    best_strike = None
    min_payout = float("inf")

    for candidate in strikes:
        payout = 0.0
        for row in chain_rows:
            strike = row.get("strike")
            if strike is None:
                continue
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}

            ce_oi = ce.get("oi") or 0
            pe_oi = pe.get("oi") or 0

            if candidate > strike:
                payout += (candidate - strike) * ce_oi  # calls ITM, writers lose
            if candidate < strike:
                payout += (strike - candidate) * pe_oi  # puts ITM, writers lose

        if payout < min_payout:
            min_payout = payout
            best_strike = candidate

    return {
        "max_pain_strike": best_strike,
        "min_aggregate_payout": round(min_payout, 0) if best_strike is not None else None,
        "note": None,
    }
