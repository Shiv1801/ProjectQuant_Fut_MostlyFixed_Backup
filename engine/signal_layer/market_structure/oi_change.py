"""
Group D: OI Change Analysis (strike-level, per expiry) — PRD §3.5-D.
Confirms institutional positioning independent of price action — large
OI builds at specific strikes flag where big players are positioning,
regardless of what price itself is doing right now.
"""


def compute_oi_change_analysis(chain_rows: list[dict], top_n: int = 5) -> dict:
    """
    chain_rows: list of {"strike":, "CE": {"oi":, "oi_change":...}, "PE": {...}}
    Fyers option chain 'oich' field = change in OI (session-level).

    Known Fyers-side limitation: the OI/OI-change fields are sometimes
    reported as flat 0 across the entire chain by Fyers itself (multiple
    independent reports of this on Fyers' own community forum, e.g.
    "Observed API's OI data as 0 for all 5 strikes"), not something our
    parsing can fix. When every non-null value is exactly 0, this is
    detected and flagged explicitly rather than silently returning a
    ranked-looking top-5 list that is actually just five zeros — a
    result that could otherwise be mistaken for a genuine "no positioning
    activity" reading rather than a data-quality gap.
    """
    builds = []
    for row in chain_rows:
        strike = row.get("strike")
        for side_label in ("CE", "PE"):
            side = row.get(side_label) or {}
            oi_change = side.get("oi_change")
            if oi_change is not None and strike is not None:
                builds.append({"strike": strike, "side": side_label, "oi_change": oi_change})

    if not builds:
        return {"top_oi_builds": [], "top_oi_unwinds": [], "note": "no OI change data available in chain", "data_quality_flag": None}

    all_zero = all(b["oi_change"] == 0 for b in builds)
    if all_zero:
        return {
            "top_oi_builds": [], "top_oi_unwinds": [],
            "note": f"all {len(builds)} OI-change values in this chain snapshot are exactly 0 — this "
                    f"is a known, occasional Fyers-side data-quality issue (their own community has "
                    f"reported OI fields returning flat 0), not a computation error. Treat this reading "
                    f"as unavailable, not as 'no positioning activity.'",
            "data_quality_flag": "all_zero_suspected_fyers_issue",
        }

    top_builds = sorted(builds, key=lambda b: b["oi_change"], reverse=True)[:top_n]
    top_unwinds = sorted(builds, key=lambda b: b["oi_change"])[:top_n]

    return {
        "top_oi_builds": top_builds,
        "top_oi_unwinds": top_unwinds,
        "note": None,
        "data_quality_flag": None,
    }
