"""
Group D: Gamma Exposure (GEX) — PRD §3.5-D. Aggregate dealer gamma
positioning across the option chain. Predicts whether dealer hedging
suppresses (high +GEX) or amplifies (negative GEX) price moves.

Standard convention: dealers are assumed long gamma from calls sold to
them (short call OI) and short gamma from puts sold to them — but the
common simplified retail approach (used here) treats call OI as +gamma
exposure and put OI as -gamma exposure, scaled by spot^2 for a dollar-
notional-style measure. This is a heuristic, not the "true" market-maker
positioning (which needs actual dealer inventory, not public OI) — flagged
in the output note so it isn't mistaken for verified dealer positioning.
"""


def compute_gex(chain_rows: list[dict], spot: float) -> dict:
    """
    chain_rows: list of {"strike": float, "CE": {..., "oi":, "delta":..}, "PE": {...}}
                as returned by our /market/option-chain endpoint, but this
                function needs gamma too — pass rows containing "gamma" per side.
    """
    if not chain_rows or spot is None or spot <= 0:
        return {"gex": None, "note": "missing chain data or spot price"}

    total_gex = 0.0
    contributing_strikes = 0

    for row in chain_rows:
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}

        ce_gamma = ce.get("gamma")
        ce_oi = ce.get("oi")
        if ce_gamma is not None and ce_oi is not None:
            total_gex += ce_gamma * ce_oi * spot * spot * 0.01
            contributing_strikes += 1

        pe_gamma = pe.get("gamma")
        pe_oi = pe.get("oi")
        if pe_gamma is not None and pe_oi is not None:
            total_gex -= pe_gamma * pe_oi * spot * spot * 0.01
            contributing_strikes += 1

    if contributing_strikes == 0:
        return {"gex": None, "note": "no strikes had both gamma and OI data"}

    if total_gex > 0:
        interpretation = "positive GEX — dealer hedging likely dampens volatility (range-bound bias)"
    else:
        interpretation = "negative GEX — dealer hedging likely amplifies moves (trend/volatility risk)"

    return {
        "gex": round(total_gex, 2),
        "contributing_strikes": contributing_strikes,
        "interpretation": interpretation,
        "note": "heuristic from public OI, not verified dealer positioning",
    }
