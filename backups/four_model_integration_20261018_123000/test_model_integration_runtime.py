import asyncio

from engine.confluence_gate.gate import evaluate_confluence
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.direction import validate_directional_candidate
from engine.execution import paper_executor, trade_dispatcher
from engine.strike_selector.selector import select_best_strike


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


def _option_chain_fixture():
    return [
        {
            "strike": 100,
            "CE": {"ltp": 10, "delta": 0.5, "symbol": "CE100"},
            "PE": {"ltp": 11, "delta": -0.5, "symbol": "PE100"},
        }
    ]


def test_bullish_direction_selects_only_a_call():
    result = select_best_strike(_option_chain_fixture(), "long_call")
    assert result["selected"]["option_type"] == "CE"
    assert validate_directional_candidate("long_call", result["selected"]["option_type"])["valid"]


def test_bearish_direction_selects_only_a_put():
    result = select_best_strike(_option_chain_fixture(), "long_put")
    assert result["selected"]["option_type"] == "PE"
    assert validate_directional_candidate("long_put", result["selected"]["option_type"])["valid"]


def test_opposite_direction_is_rejected_before_any_entry_write(monkeypatch):
    monkeypatch.setattr(paper_executor, "open_position", lambda **_: (_ for _ in ()).throw(AssertionError("must not write")))
    result = paper_executor.execute_paper_entry(
        "NIFTY50", "long_call", {"option_type": "PE", "ltp": 10, "strike": 100}, 1, 65
    )
    assert result["opened"] is False
    assert result["directional_validation"]["reason"] == "option_type_conflicts_with_quant_direction"


def test_opposite_direction_is_rejected_before_dispatch(monkeypatch):
    monkeypatch.setattr(trade_dispatcher, "is_live_trading_active_for", lambda _: (_ for _ in ()).throw(AssertionError("must not check execution mode")))
    result = trade_dispatcher.execute_entry(
        None, "NIFTY50", "PE100", "long_call", 100, "PE", 1, 65, 10
    )
    assert result["executed"] is False
    assert result["mode"] == "rejected"
    assert result["result"]["directional_validation"]["reason"] == "option_type_conflicts_with_quant_direction"


def test_selector_rejects_explicit_direction_against_gate(monkeypatch):
    import api.main as main

    async def fake_gate(*_, **__):
        return {"fired": True, "direction": "long_call", "quant_confidence": 0.8}

    monkeypatch.setattr(main, "confluence_signal", fake_gate)
    monkeypatch.setattr(main, "_parse_chain_from_shared_state", lambda _: {"spot": 100, "strikes": _option_chain_fixture()})
    monkeypatch.setattr(main, "get_iv_history", lambda **_: [])

    result = asyncio.run(main.strike_selector_signal("NIFTY50", direction="long_put"))

    assert result["status"] == "error"
    assert result["requested_direction"] == "long_put"
    assert result["direction"] == "long_call"
