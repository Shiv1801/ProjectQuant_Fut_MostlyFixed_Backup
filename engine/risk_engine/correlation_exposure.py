"""
PRD §4.8.5 — Correlation-aware exposure cap. Treats simultaneous
Nifty+BankNifty(+FinNifty) positions as one combined directional bet
when rolling correlation is high — caps combined notional, not each
index independently.
"""
def combined_exposure_cap(open_positions: list[dict], correlation_matrix: dict,
                           high_corr_threshold: float, max_combined_notional: float) -> dict:
    """
    open_positions: [{"index_id": str, "notional": float, "direction": str}]
    correlation_matrix: {"NIFTY50_BANKNIFTY": 0.82, ...}
    Returns {"breach": bool, "combined_notional": float, "correlated_group": [index_ids]}
    """
    same_direction_groups: dict[str, list[dict]] = {}
    for p in open_positions:
        same_direction_groups.setdefault(p["direction"], []).append(p)

    for direction, positions in same_direction_groups.items():
        if len(positions) < 2:
            continue
        ids = [p["index_id"] for p in positions]
        pairs_correlated = all(
            correlation_matrix.get(f"{a}_{b}", correlation_matrix.get(f"{b}_{a}", 0)) >= high_corr_threshold
            for i, a in enumerate(ids) for b in ids[i + 1:]
        )
        if pairs_correlated:
            combined_notional = sum(p["notional"] for p in positions)
            if combined_notional > max_combined_notional:
                return {"breach": True, "combined_notional": combined_notional, "correlated_group": ids}

    return {"breach": False, "combined_notional": 0.0, "correlated_group": []}
