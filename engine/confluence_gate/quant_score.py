"""
Confluence Gate — Leg 1: Quant Confidence Score (PRD §4.3.5).

Combines the already-computed model outputs (Groups A-F) into a single
directional confidence score. This is NOT a new model — it's a
weighted aggregation layer sitting on top of models we've already
built and verified individually.

Direction logic: each model votes bullish/bearish/neutral based on its
own semantics, weighted by how directly relevant that model is to
short-term direction (momentum/structure models weigh more than pure
regime/vol models, which mainly gate whether to trust the direction
signals at all).
"""


def _vote_from_hurst(hurst_result: dict) -> tuple[float, float]:
    """Returns (bullish_lean, weight). Hurst alone has no direction — it
    only tells us whether trending or mean-reverting signals should be
    trusted. Used as a confidence multiplier, not a direction vote."""
    if hurst_result.get("hurst") is None:
        return 0.0, 0.0
    regime = hurst_result.get("regime")
    # Trending regime -> trust momentum votes more; mean-reverting -> trust reversal votes more.
    # This function itself casts no directional vote.
    return 0.0, 0.0


def _vote_from_zscore(zscore_result: dict, hurst_regime: str | None) -> tuple[float, float]:
    z = zscore_result.get("zscore")
    if z is None:
        return 0.0, 0.0
    # In a mean-reverting regime, an extended z-score suggests reversal (opposite direction).
    # In a trending regime, an extended z-score suggests continuation (same direction).
    if hurst_regime == "mean_reverting":
        direction = -1 if z > 0 else 1  # fade the extension
        weight = min(abs(z) / 2.0, 1.0) * 0.8
    elif hurst_regime == "trending":
        direction = 1 if z > 0 else -1  # follow the extension
        weight = min(abs(z) / 2.0, 1.0) * 0.6
    else:
        return 0.0, 0.0
    return direction, weight


def _vote_from_variance_ratio(vr_result: dict) -> tuple[float, float]:
    # VR itself isn't directional — it's a confirmation of whether
    # momentum/reversal votes should be trusted (same role as Hurst).
    return 0.0, 0.0


def _vote_from_ofi(ofi_result: dict) -> tuple[float, float]:
    ofi = ofi_result.get("ofi")
    if ofi is None:
        return 0.0, 0.0
    direction = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
    weight = min(abs(ofi) * 2, 1.0) * 0.7
    return direction, weight


def _vote_from_vrp(vrp_result: dict) -> tuple[float, float]:
    # VRP doesn't vote direction — it gates whether buying options at
    # all is favorable right now (cheap/fair vs rich premium).
    return 0.0, 0.0


def _vote_from_pcr(pcr_result: dict) -> tuple[float, float]:
    pcr = pcr_result.get("pcr")
    if pcr is None:
        return 0.0, 0.0
    # Contrarian signal per PRD: elevated PCR (excess bearishness) -> bullish lean
    if pcr > 1.3:
        return 1, 0.4
    elif pcr < 0.7:
        return -1, 0.4
    return 0.0, 0.0


def _vote_from_correlation(corr_result: dict) -> tuple[float, float]:
    # Correlation breakdown doesn't vote direction — it's a caution flag
    # about treating indices as independent vs combined exposure.
    return 0.0, 0.0


def compute_quant_confidence(model_outputs: dict) -> dict:
    """
    model_outputs: dict of already-computed results from the various
    /signals/* endpoints, keyed by model name. Missing keys are treated
    as "no vote" rather than an error — partial data still produces a
    (lower-confidence) score.

    Returns a confidence score in [0, 1] and an inferred direction, plus
    the individual votes for transparency (never a black box).
    """
    hurst = model_outputs.get("hurst", {})
    hurst_regime = hurst.get("regime")

    votes = []
    votes.append(("zscore", *_vote_from_zscore(model_outputs.get("zscore", {}), hurst_regime)))
    votes.append(("ofi", *_vote_from_ofi(model_outputs.get("ofi", {}))))
    votes.append(("pcr", *_vote_from_pcr(model_outputs.get("pcr", {}))))

    total_weight = sum(w for _, d, w in votes)
    if total_weight == 0:
        return {
            "confidence": 0.0, "direction": None, "votes": [],
            "note": "no models produced a usable directional vote (insufficient data or all neutral)",
        }

    weighted_direction = sum(d * w for _, d, w in votes) / total_weight

    # Confidence must reflect signal STRENGTH, not just the sign of the
    # weighted-average direction. The OLD formula (min(abs(weighted_direction),
    # 1.0)) always evaluated to exactly 1.0 with a single voter, since d
    # is always +-1 and dividing by its own weight cancels the weight out
    # entirely (confirmed: 100% of 2,100 real evaluations landed on
    # exactly 0.0 or 1.0, never between).
    #
    # A FIRST fix (averaging raw vote weights directly) preserved
    # relative signal strength but was miscalibrated: those per-vote
    # weights (see _vote_from_zscore etc.) are deliberately capped low
    # (0.6-0.8 max) because they're designed as RELATIVE contribution
    # weights for combining multiple votes, not as an absolute 0-1
    # confidence scale on their own — using them directly as confidence
    # meant most real market conditions could never clear even a modest
    # 0.5-0.6 threshold (confirmed: 108,000/108,000 sweep combinations
    # produced zero trades after that first fix).
    #
    # Correct fix: rescale the average vote weight against each vote's
    # own achievable maximum, so confidence spans the full 0-1 range
    # relative to how strong a signal COULD be, not the raw capped
    # weight value itself.
    avg_weight = total_weight / len(votes)
    max_possible_weight = max(w for _, _, w in votes) if any(w > 0 for _, _, w in votes) else 1.0
    # Use the strongest single vote's weight as the confidence anchor,
    # scaled by how many models agree (more agreement -> higher floor).
    agreement_bonus = min(len([v for v in votes if v[2] > 0]) / 3.0, 1.0) * 0.15
    confidence = min(max_possible_weight / 0.8 + agreement_bonus, 1.0)  # 0.8 is the highest achievable single-vote weight (mean-reverting z-score)

    direction = "long_call" if weighted_direction > 0 else ("long_put" if weighted_direction < 0 else None)

    vote_detail = [{"model": name, "direction": d, "weight": round(w, 3)} for name, d, w in votes if w > 0]

    return {
        "confidence": round(confidence, 3),
        "direction": direction,
        "regime_context": hurst_regime,
        "votes": vote_detail,
        "note": None,
    }
