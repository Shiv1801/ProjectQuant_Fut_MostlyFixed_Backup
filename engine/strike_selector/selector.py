"""
Strike Selector — PRD §4.4. Once the confluence gate fires with a
direction, pick the best strike to actually trade. Optimizes for
expected R-multiple, not just "closest to ATM" or "highest probability
of profit" — factors delta (sweet spot band), liquidity (bid-ask
spread), and IV rank (avoid buying already-rich premium).
"""

DELTA_TARGET_RANGE = (0.30, 0.40)  # starting hypothesis per PRD — needs backtest validation
MAX_SPREAD_PCT = 3.0               # max acceptable bid-ask spread as % of mid price
MAX_IV_RANK = 70                   # avoid strikes where IV is already in a high historical percentile


def _delta_fit_score(delta: float | None) -> float:
    if delta is None:
        return 0.0
    abs_delta = abs(delta)
    target_mid = sum(DELTA_TARGET_RANGE) / 2
    target_width = (DELTA_TARGET_RANGE[1] - DELTA_TARGET_RANGE[0]) / 2
    distance = abs(abs_delta - target_mid)
    return max(0.0, 1.0 - distance / (target_width * 2))


def _liquidity_fit_score(bid: float | None, ask: float | None) -> float:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return 0.0
    mid = (bid + ask) / 2
    if mid == 0:
        return 0.0
    spread_pct = (ask - bid) / mid * 100
    return max(0.0, 1.0 - spread_pct / MAX_SPREAD_PCT)


def _iv_rank_fit_score(iv_rank: float | None) -> float:
    if iv_rank is None:
        return 0.5  # neutral — no penalty/bonus if we don't have IV rank data
    if iv_rank > MAX_IV_RANK:
        return 0.0
    return 1.0 - (iv_rank / MAX_IV_RANK) * 0.5  # mild preference for lower IV rank, not a hard cutoff


def score_strike(strike_data: dict, iv_rank: float | None = None) -> dict:
    """
    strike_data: one side (CE or PE) of a strike from our option chain
    parsing, e.g. {"ltp":, "delta":, "gamma":, "oi":, ...}. Needs bid/ask
    for liquidity scoring — if the chain data doesn't carry those, this
    degrades gracefully (liquidity score becomes neutral/0).
    """
    delta = strike_data.get("delta")
    bid = strike_data.get("bid")
    ask = strike_data.get("ask")

    delta_score = _delta_fit_score(delta)
    liquidity_score = _liquidity_fit_score(bid, ask)
    iv_score = _iv_rank_fit_score(iv_rank)

    composite = 0.45 * delta_score + 0.30 * liquidity_score + 0.25 * iv_score

    return {
        "composite_score": round(composite, 3),
        "delta_score": round(delta_score, 3),
        "liquidity_score": round(liquidity_score, 3),
        "iv_rank_score": round(iv_score, 3),
        "delta": delta,
    }


def select_best_strike(strikes: list[dict], direction: str, iv_rank: float | None = None,
                        max_entry_iv_rank: float | None = None) -> dict:
    """
    strikes: our parsed option-chain strike list, each {"strike":, "CE": {...}, "PE": {...}}.
    direction: "long_call" -> only consider CE side, "long_put" -> only PE side.
    max_entry_iv_rank: optional hard gate — if the current IV rank exceeds
        this, no strike is selected at all (skip the trade entirely, don't
        just soft-penalize it). None (default) disables the gate, preserving
        existing soft-scoring-only behavior.
    """
    if max_entry_iv_rank is not None and iv_rank is not None and iv_rank > max_entry_iv_rank:
        return {"selected": None, "note": f"IV rank {iv_rank} exceeds max_entry_iv_rank gate ({max_entry_iv_rank}) — trade skipped"}

    if not strikes:
        return {"selected": None, "note": "no strikes available"}

    side = "CE" if direction == "long_call" else "PE"
    candidates = []

    for row in strikes:
        option_data = row.get(side)
        if not option_data:
            continue
        # Skip strikes with no real price (illiquid/far OTM junk)
        if not option_data.get("ltp") or option_data["ltp"] <= 0:
            continue
        score_result = score_strike(option_data, iv_rank=iv_rank)
        candidates.append({
            "strike": row["strike"],
            "option_type": side,
            "ltp": option_data.get("ltp"),
            "symbol": option_data.get("symbol"),
            **score_result,
        })

    if not candidates:
        return {"selected": None, "note": f"no valid {side} candidates found in chain"}

    candidates.sort(key=lambda c: c["composite_score"], reverse=True)
    best = candidates[0]

    return {
        "selected": best,
        "candidates_considered": len(candidates),
        "top_3_alternatives": candidates[1:4],
        "note": None,
    }
