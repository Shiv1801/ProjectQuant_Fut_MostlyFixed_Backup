from engine.confluence_gate.gate import evaluate_confluence
from engine.confluence_gate.quant_score import compute_quant_confidence


def _directional_snapshot():
    return {
        "hurst": {"hurst": 0.6, "regime": "trending", "status": "ok"},
        "zscore": {"zscore": 2.0, "status": "ok"},
        "ofi": {"ofi": 0.5, "status": "ok"},
        "pcr": {"pcr": 2.0, "status": "ok"},
        "max_pain": {"max_pain_strike": 110, "status": "ok"},
        "oi_change": {"top_oi_builds": [{"side": "PE", "oi_change": 5_000_000}], "status": "ok"},
        "group_g": {"active": True, "score": 0.7, "model_beats_baseline": True, "status": "ok"},
        "gex": {"gex": 1000, "status": "ok"},
        "variance_ratio": {"variance_ratio": 1.2, "status": "ok"},
    }


def test_quant_score_reports_actual_contributors_and_exclusions():
    result = compute_quant_confidence(_directional_snapshot(), evaluation_id="eval-1")

    assert result["evaluation_id"] == "eval-1"
    assert result["direction_source"] == "quant_score"
    assert result["model_integration"]["zscore"]["contributes_to_score"] is True
    assert result["model_integration"]["gex"]["state"] == "diagnostic_only"
    assert result["model_integration"]["variance_ratio"]["state"] == "context_only"
    assert result["model_integration"]["realized_vol"]["state"] == "not_evaluated"


def test_missing_direction_is_explicitly_not_trade_eligible():
    result = compute_quant_confidence({"hurst": {"regime": "random_walk", "status": "ok"}})
    gate = evaluate_confluence(result, {"verdict": "confirmed", "confirmed": True})

    assert result["direction"] is None
    assert result["directional_alignment"]["status"] == "neutral_blocks_trade"
    assert gate["fired"] is False
    assert gate["directional_alignment"]["status"] == "neutral_blocks_trade"


def test_gate_preserves_only_the_quant_direction():
    result = compute_quant_confidence({
        "hurst": {"regime": "trending", "status": "ok"},
        "zscore": {"zscore": -2.0, "status": "ok"},
    })
    gate = evaluate_confluence(result, {"verdict": "confirmed", "confirmed": True}, require_chart=False)

    assert result["direction"] == "long_put"
    assert gate["fired"] is True
    assert gate["direction"] == "long_put"
    assert gate["directional_alignment"]["trade_direction"] == "long_put"
    assert gate["directional_alignment"]["opposite_direction_rejected"] is True


def test_unavailable_directional_input_has_no_score_vote():
    result = compute_quant_confidence({"ofi": {"status": "unavailable", "ofi": None}})

    assert result["direction"] is None
    assert result["model_integration"]["ofi"]["state"] == "unavailable"
    assert result["model_integration"]["ofi"]["score_vote"] is None
