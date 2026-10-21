from engine.confluence_gate.quant_score import compute_quant_confidence


def test_existing_directional_outputs_reach_the_approved_quant_aggregator():
    result = compute_quant_confidence({
        "hurst": {"regime": "trending", "hurst": 0.6},
        "zscore": {"zscore": 2.0},
        "ofi": {"ofi": 0.5},
        "pcr": {"pcr": 2.0},
        "max_pain": {"max_pain_strike": 110},
        "oi_change": {"top_oi_builds": [{"side": "PE", "oi_change": 5_000_000}]},
        "spot": 100,
        "group_g": {"active": True, "score": 0.7, "model_beats_baseline": True},
        "gex": {"gex": 1_000},
        "variance_ratio": {"variance_ratio": 1.2},
    })
    vote_names = {vote["model"] for vote in result["votes"]}
    assert {"zscore", "ofi", "pcr", "max_pain", "oi_change", "group_g"} <= vote_names
    assert "gex" not in vote_names
    assert "variance_ratio" not in vote_names
