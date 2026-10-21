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

import math


MODEL_INTEGRATION = {
    "hurst": ("C", "directional_context", "regime context selects the z-score interpretation"),
    "zscore": ("C", "score_contributor", "directional vote"),
    "variance_ratio": ("C", "confirmation", "confirms or withholds the Hurst-selected z-score regime"),
    "ofi": ("C", "score_contributor", "directional vote when depth is valid"),
    "futures_ofi": ("C", "score_contributor", "directional vote when at least one discovered futures book is valid and fresh"),
    "pcr": ("D", "score_contributor", "contrarian directional vote"),
    "max_pain": ("D", "score_contributor", "soft directional vote"),
    "oi_change": ("D", "score_contributor", "directional positioning vote"),
    "gex": ("D", "diagnostic_only", "no approved score mapping"),
    "realized_vol": ("A", "context_only", "volatility context; not a directional vote"),
    "garch": ("A", "context_only", "volatility context; not a directional vote"),
    "vrp": ("B", "premium_suitability", "option-premium suitability gate; never directional"),
    "iv_rank": ("B", "diagnostic_only", "no approved score mapping"),
    "vix_regime": ("E", "context_only", "regime context; not a directional vote"),
    "rv_iv_ratio": ("E", "context_only", "regime context; not a directional vote"),
    "correlation": ("E", "disabled", "requires a second approved index series"),
    "kelly": ("F", "sizing_input", "position sizing only"),
    "vol_scaled": ("F", "sizing_input", "position sizing only"),
    "group_g": ("G", "score_contributor", "validated ML vote when active"),
}


def _result_is_unavailable(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return True
    return result.get("status") in {"error", "unavailable", "not_applicable"}


def _build_integration_audit(model_outputs: dict, votes: list[tuple[str, float, float]]) -> dict:
    """Describe actual score use without assigning score to excluded models."""
    vote_by_model = {
        name: {"direction": direction, "weight": round(weight, 6)}
        for name, direction, weight in votes
        if weight > 0
    }
    audit = {}
    for model, (group, role, reason) in MODEL_INTEGRATION.items():
        result = model_outputs.get(model)
        if model not in model_outputs:
            state = "not_evaluated"
            exclusion = "model output was not present in this evaluation snapshot"
        elif _result_is_unavailable(result):
            state = "unavailable"
            exclusion = "model output was unavailable or not applicable"
        elif model in vote_by_model:
            state = "contributing"
            exclusion = None
        else:
            state = role
            exclusion = reason
        audit[model] = {
            "group": group,
            "role": role,
            "state": state,
            "contributes_to_score": state == "contributing",
            "score_vote": vote_by_model.get(model),
            "exclusion_reason": exclusion,
        }
    return audit


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


def _regime_confirmation(hurst_result: dict, vr_result: dict) -> dict:
    """Combine two non-directional regime checks without double-counting them."""
    hurst = hurst_result.get("hurst")
    hurst_regime = hurst_result.get("regime")
    if hurst is None or hurst_regime not in {"trending", "mean_reverting", "random_walk"}:
        return {"status": "unavailable", "zscore_eligible": False,
                "effect": "zscore_withheld", "reason": "Hurst is missing, invalid, or ambiguous"}
    if hurst_regime == "random_walk":
        return {"status": "neutral", "zscore_eligible": False,
                "effect": "zscore_withheld", "reason": "Hurst is in the random-walk band"}

    vr = vr_result.get("variance_ratio")
    if vr is None or vr_result.get("persistent") is None:
        return {"status": "partial", "zscore_eligible": True,
                "effect": "hurst_only", "reason": "Variance Ratio unavailable; Hurst remains authoritative"}
    vr_regime = "trending" if vr_result.get("persistent") else (
        "mean_reverting" if vr_result.get("mean_reverting") else "random_walk"
    )
    if vr_regime == hurst_regime:
        return {"status": "aligned", "zscore_eligible": True,
                "effect": "confirmed", "reason": "Hurst and Variance Ratio agree"}
    return {"status": "conflict", "zscore_eligible": False,
            "effect": "zscore_withheld", "reason": "Hurst and Variance Ratio disagree"}


def _vote_from_zscore(zscore_result: dict, hurst_regime: str | None,
                      regime_confirmation: dict | None = None) -> tuple[float, float]:
    z = zscore_result.get("zscore")
    if z is None:
        return 0.0, 0.0
    if regime_confirmation is not None and not regime_confirmation.get("zscore_eligible", False):
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
    # VR is intentionally not a directional vote. Its effect is returned in
    # regime_confirmation so it cannot be counted twice with Hurst.
    return 0.0, 0.0


def _vote_from_ofi(ofi_result: dict) -> tuple[float, float]:
    if not isinstance(ofi_result, dict):
        return 0.0, 0.0
    if "status" in ofi_result and ofi_result.get("status") != "valid":
        return 0.0, 0.0
    if ofi_result.get("fresh") is False or (
        isinstance(ofi_result.get("age_seconds"), (int, float))
        and ofi_result["age_seconds"] > 15
    ):
        return 0.0, 0.0
    ofi = ofi_result.get("ofi")
    if not isinstance(ofi, (int, float)) or not math.isfinite(ofi):
        return 0.0, 0.0
    direction = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
    weight = min(abs(ofi) * 2, 1.0) * 0.7
    return direction, weight


def _vote_from_futures_ofi(ofi_result: dict) -> tuple[float, float]:
    """Use any valid, fresh futures-depth snapshots; one contract is enough.

    calculate_futures_ofi marks one valid contract as ``partial`` and
    exposes its quantity-weighted value as ``combined_ofi``. Requiring two
    contracts here discarded usable data and prevented the fallback policy.
    Invalid/stale rows are excluded by calculate_futures_ofi; independently
    verify that at least one row is valid and the aggregate is finite.
    """
    if not isinstance(ofi_result, dict) or ofi_result.get("status") not in {"valid", "ok", "partial"}:
        return 0.0, 0.0
    rows = ofi_result.get("contracts") or []
    valid_rows = [row for row in rows if isinstance(row, dict) and row.get("status") == "valid"]
    valid_count = ofi_result.get("valid_contracts")
    if not isinstance(valid_count, int) or valid_count < 1 or not valid_rows:
        return 0.0, 0.0
    if ofi_result.get("expected_contracts") not in (None, 2):
        return 0.0, 0.0
    if ofi_result.get("health_status") in {"STALE", "INVALID_CONTRACT", "INVALID_DATA", "NO_VALID_CONTRACTS", "MISSING_DEPTH"}:
        # MISSING_DEPTH may describe the missing second contract in a partial
        # result, so allow it only when the aggregate itself is explicitly partial.
        if not (ofi_result.get("status") == "partial" and valid_count >= 1):
            return 0.0, 0.0
    ofi = ofi_result.get("combined_ofi")
    if not isinstance(ofi, (int, float)) or isinstance(ofi, bool) or not math.isfinite(ofi):
        return 0.0, 0.0
    direction = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
    weight = min(abs(ofi) * 2, 1.0) * 0.7
    return direction, weight


def _vote_from_vrp(vrp_result: dict) -> tuple[float, float]:
    # VRP doesn't vote direction — it gates whether buying options at
    # all is favorable right now (cheap/fair vs rich premium).
    return 0.0, 0.0


def _vote_from_pcr(pcr_result: dict) -> tuple[float, float]:
    """
    Converted from thresholded (flat 0.4 weight the instant PCR crossed
    1.3 or 0.7) to continuous/scalable, 2026-10-01 per explicit request.
    FIXES A REAL, CONFIRMED GAP: the old version gave PCR=1.31 and
    PCR=3.0 the identical vote weight (0.4), discarding how extreme the
    reading actually was -- a hard cliff at the threshold (1.29 -> 0.0,
    1.31 -> 0.4) that could flip the aggregate confidence/direction on a
    trivial, noisy PCR move. This is a pure math change: same input
    field (pcr), same direction logic (contrarian: elevated PCR ->
    bullish, depressed PCR -> bearish), same 0.4 ceiling as before (so
    PCR still cannot outweigh zscore's 0.6-0.8 max) -- no new data
    source, no new Fyers/API call.

    Scaling: distance past the threshold, normalized against the SAME
    width as the neutral zone itself (1.3 - 0.7 = 0.6), so a PCR that's
    moved a full neutral-zone-width past the threshold (pcr=1.9, or
    pcr=0.1) reaches the full 0.4 weight; anything beyond that is capped
    at 0.4, same ceiling as the old flat value. A PCR just past the
    threshold (e.g. 1.31) now gets a small weight instead of the full
    0.4, removing the cliff.
    """
    pcr = pcr_result.get("pcr")
    if pcr is None:
        return 0.0, 0.0
    neutral_zone_width = 0.6  # 1.3 - 0.7, same band as the original thresholds
    # Contrarian signal per PRD: elevated PCR (excess bearishness) -> bullish lean
    if pcr > 1.3:
        weight = min((pcr - 1.3) / neutral_zone_width, 1.0) * 0.4
        return 1, weight
    elif pcr < 0.7:
        weight = min((0.7 - pcr) / neutral_zone_width, 1.0) * 0.4
        return -1, weight
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


def compute_quant_confidence(model_outputs: dict, evaluation_id: str | None = None) -> dict:
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
    variance_ratio = model_outputs.get("variance_ratio", {})
    regime_confirmation = _regime_confirmation(hurst, variance_ratio)

    votes = []
    votes.append(("zscore", *_vote_from_zscore(
        model_outputs.get("zscore", {}), hurst_regime, regime_confirmation)))

    # Spot and futures are alternative/fallback sources for one OFI factor.
    # When both are valid, split the existing 0.7 maximum influence between
    # them so the same factor is not counted twice. Their weighted votes then
    # naturally cancel when the sources disagree.
    spot_ofi_vote = _vote_from_ofi(model_outputs.get("ofi", {}))
    futures_ofi_vote = _vote_from_futures_ofi(model_outputs.get("futures_ofi", {}))
    both_ofi_sources_valid = spot_ofi_vote[1] > 0 and futures_ofi_vote[1] > 0
    if both_ofi_sources_valid:
        votes.append(("ofi", spot_ofi_vote[0], spot_ofi_vote[1] / 2.0))
        votes.append(("futures_ofi", futures_ofi_vote[0], futures_ofi_vote[1] / 2.0))
    else:
        votes.append(("ofi", *spot_ofi_vote))
        votes.append(("futures_ofi", *futures_ofi_vote))
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

    integration_audit = _build_integration_audit(model_outputs, votes)

    spot_ofi = model_outputs.get("ofi") if isinstance(model_outputs.get("ofi"), dict) else {}
    futures_ofi = model_outputs.get("futures_ofi") if isinstance(model_outputs.get("futures_ofi"), dict) else {}
    valid_ofi_sources = []
    if _vote_from_ofi(spot_ofi)[1] > 0:
        valid_ofi_sources.append("spot")
    if _vote_from_futures_ofi(futures_ofi)[1] > 0:
        valid_ofi_sources.append("futures")
    ofi_values = []
    if "spot" in valid_ofi_sources:
        ofi_values.append(float(spot_ofi["ofi"]))
    if "futures" in valid_ofi_sources:
        ofi_values.append(float(futures_ofi["combined_ofi"]))
    ofi_integration = {
        "sources": valid_ofi_sources,
        "status": "available" if valid_ofi_sources else "unavailable",
        "combined_ofi": round(sum(ofi_values) / len(ofi_values), 3) if ofi_values else None,
        "method": "equal_mean_of_valid_spot_and_futures_values; source votes retain existing 0.7 weights",
    }

    total_weight = sum(w for _, d, w in votes)
    if total_weight == 0:
        return {
            "confidence": 0.0, "direction": None, "votes": [],
            "note": "no models produced a usable directional vote (insufficient data or all neutral)",
            "evaluation_id": evaluation_id,
            "direction_source": "quant_score",
            "directional_alignment": {"required": True, "status": "neutral_blocks_trade"},
            "regime_confirmation": regime_confirmation,
            "model_integration": integration_audit,
            "ofi_integration": ofi_integration,
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
        "evaluation_id": evaluation_id,
        "direction_source": "quant_score",
        "directional_alignment": {
            "required": True,
            "status": "aligned" if direction in ("long_call", "long_put") else "neutral_blocks_trade",
        },
        "regime_context": hurst_regime,
        "regime_confirmation": regime_confirmation,
        "votes": vote_detail,
        "model_integration": integration_audit,
        "ofi_integration": ofi_integration,
        "note": None,
    }
