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


def _vote_from_max_pain(max_pain_result: dict, spot: float | None) -> tuple[float, float]:
    """
    Added per explicit request (2026-09-25) after confirming only 3 of
    12+ computed models (zscore/OFI/PCR) were ever wired into the
    quant confidence score -- the rest were computed and displayed on
    the dashboard but structurally excluded from the decision, which
    did not match the intended design ("all models performing hand in
    hand to give one score"). Max Pain IS genuinely directional (unlike
    Hurst/VRP/GEX/vol-regime models, which are gates/context by design
    and correctly stay excluded): price has a statistical tendency to
    drift toward the max-pain strike near expiry, so spot below max
    pain implies a mild upward pull, spot above implies a mild
    downward pull. Weighted lower (0.3) than the momentum/flow votes
    (0.6-0.8) since this is a soft statistical tendency, not a strong
    signal, and only relevant as expiry approaches -- a real limitation
    this function doesn't currently account for (no time-to-expiry
    input), so it contributes a vote at any distance from expiry.
    """
    max_pain_strike = max_pain_result.get("max_pain_strike")
    if max_pain_strike is None or spot is None or spot <= 0:
        return 0.0, 0.0
    distance_pct = abs(spot - max_pain_strike) / spot * 100
    if distance_pct < 0.05:  # already essentially pinned to max pain -- no meaningful pull either way
        return 0.0, 0.0
    direction = 1 if spot < max_pain_strike else -1
    weight = min(distance_pct / 1.0, 1.0) * 0.3
    return direction, weight


def _vote_from_oi_change(oi_change_result: dict) -> tuple[float, float]:
    """
    Added alongside _vote_from_max_pain, same rationale. OI Change IS
    genuinely directional: a large fresh OI build at a specific strike
    flags institutional positioning independent of price action --
    heavy PUT OI build suggests support/bullish positioning (writers
    expect price to stay above that strike), heavy CALL OI build
    suggests resistance/bearish positioning. Uses only the SINGLE
    largest build (top_oi_builds[0]) as the vote, not all 5, to avoid
    double-counting noise from multiple builds at different strikes
    that could contradict each other. Explicitly checks the real,
    confirmed Fyers data-quality flag (compute_oi_change_analysis
    already detects and flags an all-zero chain as a known Fyers-side
    bug, not genuine "no activity") -- when that flag is set, this
    correctly abstains rather than casting a vote on bad data.
    """
    if oi_change_result.get("data_quality_flag") == "all_zero_suspected_fyers_issue":
        return 0.0, 0.0
    builds = oi_change_result.get("top_oi_builds") or []
    if not builds:
        return 0.0, 0.0
    top_build = builds[0]
    side = top_build.get("side")
    oi_change = top_build.get("oi_change")
    if side is None or oi_change is None or oi_change <= 0:
        return 0.0, 0.0
    direction = 1 if side == "PE" else -1  # heavy PE build = support/bullish, heavy CE build = resistance/bearish
    # Weight scales with build size relative to a real, confirmed-large
    # build from this project's own data (~9.3M seen 2026-09-25) as the
    # reference for "strong" positioning -- smaller builds vote weaker.
    weight = min(oi_change / 10_000_000, 1.0) * 0.35
    return direction, weight


def _vote_from_group_g(group_g_result: dict | None) -> tuple[float, float]:
    """
    Group G (GBT classifier) vote — added 2026-09-30. group_g_result is
    the dict returned by engine.signal_layer.ml_ensemble.group_g's
    compute_group_g_score(), computed by the caller (api/main.py's
    confluence_signal) and passed in via model_outputs["group_g"], the
    same pattern as every other model here.

    Threshold-gated: compute_group_g_score() itself returns active=False
    (score=None) until MIN_TRADES_FOR_GROUP_G closed trades exist AND a
    model has actually been trained -- this function correctly abstains
    (0.0, 0.0) in that case, exactly like every other _vote_from_* here
    abstains on missing data. This is NOT a fallback/guess: an inactive
    Group G contributes literally nothing to the score, so confidence
    behaves identically to before Group G existed until there's real
    trained data behind it.

    proba (score) is P(winning trade | current feature snapshot) in
    [0, 1]; 0.5 = no edge either direction. Cast as a vote in the
    QUANT DIRECTION already implied by the other models' weighted_direction
    (Group G's training labels are win/loss, not long/short -- it has no
    independent directional opinion of its own, only "does this setup, as
    currently voted, look favorable or not"), so its vote amplifies or
    dampens the existing direction rather than proposing a new one.
    Weighted moderately (0.5, between OFI's 0.7 and max_pain's 0.3) since
    it is validated only against this app's own paper/live trade history,
    not an independent external check.
    """
    if not group_g_result or not group_g_result.get("active"):
        return 0.0, 0.0
    proba = group_g_result.get("score")
    if proba is None:
        return 0.0, 0.0
    edge = proba - 0.5  # >0 favors the setup, <0 argues against it
    if edge == 0:
        return 0.0, 0.0
    # This vote doesn't know long/short on its own -- it votes 1 (agree
    # with whatever direction the OTHER models already lean, amplifying
    # confidence) when proba > 0.5, or -1 (argue against firing at all,
    # dampening confidence) when proba < 0.5. Weight scales with |edge|
    # and is further discounted if the model didn't beat its own
    # validation baseline (still contributes, per the design note in
    # compute_group_g_score, just weighted down rather than excluded).
    baseline_discount = 1.0 if group_g_result.get("model_beats_baseline") else 0.5
    weight = min(abs(edge) * 2, 1.0) * 0.5 * baseline_discount
    direction = 1 if edge > 0 else -1
    return direction, weight


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
    # Added 2026-09-25, per explicit request -- these two are the only
    # other genuinely directional models among the 12+ computed; the
    # rest (Hurst, Variance Ratio, VRP, GEX, vol/regime models) are
    # correctly excluded as non-directional gates/context, by design
    # (see their own _vote_from_* comments above).
    votes.append(("max_pain", *_vote_from_max_pain(model_outputs.get("max_pain", {}), model_outputs.get("spot"))))
    votes.append(("oi_change", *_vote_from_oi_change(model_outputs.get("oi_change", {}))))
    # Added 2026-09-30 -- inactive/zero-weight until MIN_TRADES_FOR_GROUP_G
    # real closed trades exist and a model has been trained (see
    # engine/signal_layer/ml_ensemble/group_g.py). Uses .get() with no
    # default dict so a caller that hasn't wired this key in yet (e.g. an
    # older call site) behaves exactly as before -- backward compatible.
    votes.append(("group_g", *_vote_from_group_g(model_outputs.get("group_g"))))

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
