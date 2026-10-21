from engine.confluence_gate.gate import evaluate_confluence
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.ofi import calculate_futures_ofi
from engine.signal_layer.momentum.variance_ratio import compute_variance_ratio
from engine.signal_layer.vrp_pricing.vrp import compute_vrp


def test_hurst_regime_controls_zscore_without_becoming_a_vote():
    trending = compute_quant_confidence({
        "hurst": {"hurst": 0.6, "regime": "trending"},
        "variance_ratio": {"variance_ratio": 1.2, "persistent": True, "mean_reverting": False},
        "zscore": {"zscore": 2.0},
    })
    mean_reverting = compute_quant_confidence({
        "hurst": {"hurst": 0.4, "regime": "mean_reverting"},
        "variance_ratio": {"variance_ratio": 0.8, "persistent": False, "mean_reverting": True},
        "zscore": {"zscore": 2.0},
    })

    assert trending["direction"] == "long_call"
    assert mean_reverting["direction"] == "long_put"
    assert trending["model_integration"]["hurst"]["contributes_to_score"] is False
    assert trending["regime_confirmation"]["effect"] == "confirmed"


def test_hurst_ambiguous_or_conflicting_variance_ratio_withholds_zscore():
    ambiguous = compute_quant_confidence({
        "hurst": {"hurst": 0.5, "regime": "random_walk"},
        "variance_ratio": {"variance_ratio": 1.2, "persistent": True, "mean_reverting": False},
        "zscore": {"zscore": 2.0},
    })
    conflicting = compute_quant_confidence({
        "hurst": {"hurst": 0.6, "regime": "trending"},
        "variance_ratio": {"variance_ratio": 0.8, "persistent": False, "mean_reverting": True},
        "zscore": {"zscore": 2.0},
    })

    assert ambiguous["direction"] is None
    assert ambiguous["regime_confirmation"]["status"] == "neutral"
    assert conflicting["direction"] is None
    assert conflicting["regime_confirmation"]["status"] == "conflict"
    assert conflicting["model_integration"]["variance_ratio"]["contributes_to_score"] is False


def test_variance_ratio_insufficient_data_is_explicit():
    result = compute_variance_ratio([100.0] * 20)
    assert result["variance_ratio"] is None
    assert "need at least" in result["note"]


def test_hurst_insufficient_data_is_explicit():
    result = compute_hurst_exponent([100.0] * 10)
    assert result["hurst"] is None
    assert result["regime"] is None


def test_vrp_uses_same_annualized_percentage_units_and_never_votes():
    favorable = compute_vrp(12.0, 10.0)
    expensive = compute_vrp(15.0, 10.0)
    missing = compute_vrp(None, 10.0)

    assert favorable == {
        "vrp_pct": 2.0,
        "atm_iv_pct": 12.0,
        "garch_forecast_vol_pct": 10.0,
        "favorable": True,
        "note": "options fairly/cheaply priced — favorable to buy",
    }
    assert expensive["favorable"] is False
    assert missing["vrp_pct"] is None
    assert compute_quant_confidence({"vrp": favorable})["direction"] is None


def test_vrp_suitability_gate_is_explicit_and_optional():
    quant = {"confidence": 0.9, "direction": "long_call", "direction_source": "quant_score"}
    chart = {"verdict": "confirmed", "confirmed": True}
    rejected = evaluate_confluence(
        quant, chart, require_chart=True, require_vrp=True,
        vrp_result={"vrp_pct": 3.0, "favorable": False},
    )
    unavailable = evaluate_confluence(
        quant, chart, require_chart=True, require_vrp=True,
        vrp_result={"vrp_pct": None, "favorable": None},
    )
    optional = evaluate_confluence(
        quant, chart, require_chart=True, require_vrp=False,
        vrp_result={"vrp_pct": 3.0, "favorable": False},
    )

    assert rejected["veto_reason"] == "vrp_unfavorable"
    assert unavailable["veto_reason"] == "vrp_unavailable"
    assert optional["fired"] is True


def _futures_ofi(status="valid", current_buy=700, current_sell=300,
                 next_buy=600, next_sell=400):
    result = calculate_futures_ofi([
        {"symbol": "NSE:NIFTY26OCTFUT", "expiry": "oct", "updated_at": "2026-10-18T12:00:00+00:00",
         "total_buy_qty": current_buy, "total_sell_qty": current_sell},
        {"symbol": "NSE:NIFTY26NOVFUT", "expiry": "nov", "updated_at": "2026-10-18T12:00:00+00:00",
         "total_buy_qty": next_buy, "total_sell_qty": next_sell},
    ], now=__import__("datetime").datetime(2026, 10, 18, 12, 0, 5,
                                             tzinfo=__import__("datetime").timezone.utc))
    if status == "stale":
        result["status"] = "stale"
    return result


def test_futures_ofi_valid_two_contract_aggregate_votes_and_preserves_symbols():
    result = _futures_ofi()
    scored = compute_quant_confidence({"futures_ofi": result})
    vote = next(v for v in scored["votes"] if v["model"] == "futures_ofi")

    assert result["status"] == "valid"
    assert {row["symbol"] for row in result["contracts"]} == {
        "NSE:NIFTY26OCTFUT", "NSE:NIFTY26NOVFUT"
    }
    assert vote["direction"] == 1
    assert vote["weight"] == 0.42


def test_futures_ofi_stale_or_partial_data_is_excluded_not_neutral():
    stale = _futures_ofi(status="stale")
    stale_score = compute_quant_confidence({"futures_ofi": stale})
    partial = _futures_ofi()
    partial["status"] = "partial"
    partial["valid_contracts"] = 1
    partial_score = compute_quant_confidence({"futures_ofi": partial})

    assert not any(v["model"] == "futures_ofi" for v in stale_score["votes"])
    assert not any(v["model"] == "futures_ofi" for v in partial_score["votes"])
    assert stale_score["model_integration"]["futures_ofi"]["state"] == "score_contributor"


def test_all_four_models_are_snapshot_visible_with_distinct_roles():
    result = compute_quant_confidence({
        "hurst": {"hurst": 0.6, "regime": "trending"},
        "variance_ratio": {"variance_ratio": 1.2, "persistent": True, "mean_reverting": False},
        "zscore": {"zscore": 2.0},
        "vrp": {"vrp_pct": 1.0, "favorable": True},
        "futures_ofi": _futures_ofi(),
    }, evaluation_id="eval-four-models")

    assert result["evaluation_id"] == "eval-four-models"
    assert result["model_integration"]["hurst"]["role"] == "directional_context"
    assert result["model_integration"]["variance_ratio"]["role"] == "confirmation"
    assert result["model_integration"]["vrp"]["role"] == "premium_suitability"
    assert result["model_integration"]["futures_ofi"]["contributes_to_score"] is True
    assert len([v for v in result["votes"] if v["model"] == "futures_ofi"]) == 1
