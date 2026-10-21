from engine.confluence_gate.direction import validate_entry_candidate
from engine.confluence_gate.gate import evaluate_confluence
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.execution import live_executor, trade_dispatcher


def fired(direction="long_call", confidence=0.9):
    return evaluate_confluence(
        {"direction": direction, "confidence": confidence},
        {"verdict": "confirms", "confirmed": True, "fresh": True},
        min_confidence_threshold=0.8,
        require_chart=True,
        require_vwap=False,
    )


def test_bullish_and_bearish_entries_map_only_to_buy_option_types():
    assert validate_entry_candidate("long_call", "CE", "NSE:NIFTY26OCT25000CE", "NIFTY50")["valid"]
    assert validate_entry_candidate("long_put", "PE", "NSE:NIFTY26OCT25000PE", "NIFTY50")["valid"]
    assert not validate_entry_candidate("long_call", "PE", "NSE:NIFTY26OCT25000PE", "NIFTY50")["valid"]
    assert not validate_entry_candidate("long_put", "CE", "NSE:NIFTY26OCT25000CE", "NIFTY50")["valid"]


def test_invalid_or_neutral_quant_result_fails_closed():
    assert fired("neutral", 0.99)["fired"] is False
    assert fired("long_call", None)["fired"] is False
    assert fired("long_call", 0.79)["fired"] is False


def test_required_chart_and_vwap_data_cannot_be_missing_or_stale():
    opposite = evaluate_confluence(
        {"direction": "long_call", "confidence": 0.9},
        {"verdict": "opposite", "confirmed": False, "fresh": True},
        min_confidence_threshold=0.8,
    )
    assert opposite["fired"] is False

    stale_vwap = evaluate_confluence(
        {"direction": "long_call", "confidence": 0.9},
        {"verdict": "confirms", "confirmed": True, "fresh": True},
        min_confidence_threshold=0.8,
        require_vwap=True,
        vwap_result={"available": True, "vwap": 100, "price": 101, "status": "STALE_CURRENT_CANDLE"},
    )
    assert stale_vwap["fired"] is False
    assert stale_vwap["veto_reason"] == "vwap_unavailable_or_stale"


def test_futures_ofi_is_a_real_quant_vote_and_changes_score():
    base = {
        "hurst": {"hurst": 0.6, "regime": "trending"},
        "zscore": {"zscore": 0},
    }

    without_ofi = compute_quant_confidence(base)

    futures_ofi = {
        "status": "valid",
        "combined_ofi": 0.8,
        "valid_contracts": 2,
        "expected_contracts": 2,
        "health_status": "AVAILABLE",
        "contracts": [
            {"status": "valid", "ofi": 0.8},
            {"status": "valid", "ofi": 0.8},
        ],
    }

    with_ofi = compute_quant_confidence({
        **base,
        "futures_ofi": futures_ofi,
    })

    assert any(
        vote["model"] == "futures_ofi"
        for vote in with_ofi["votes"]
    )
    assert with_ofi["confidence"] != without_ofi["confidence"]
def test_mandatory_futures_ofi_unavailable_fails_closed():
    result = evaluate_confluence(
        {"direction": "long_call", "confidence": 0.9},
        {"verdict": "confirms", "confirmed": True, "fresh": True},
        min_confidence_threshold=0.8,
        require_futures_ofi=True,
        futures_ofi_result={"status": "unavailable", "combined_ofi": None},
    )
    assert result["fired"] is False
    assert result["veto_reason"] == "futures_ofi_unavailable"


def test_live_entry_side_override_is_rejected_without_broker_call(monkeypatch):
    monkeypatch.setattr(live_executor, "get_fyers_model", lambda *_: (_ for _ in ()).throw(AssertionError("broker must not be called")))
    result = live_executor.place_live_entry_order(
        "token", "NIFTY50", "NSE:NIFTY26OCT25000CE", 65,
        "long_call", 25000, "CE", 1, 65, side=live_executor.SIDE_SELL,
        authorization={"fired": True, "direction": "long_call"},
    )
    assert result["status"] == "rejected"


def test_dispatcher_requires_the_authoritative_fired_gate(monkeypatch):
    monkeypatch.setattr(trade_dispatcher, "is_live_trading_active_for", lambda _: False)
    monkeypatch.setattr(trade_dispatcher, "open_position", lambda **_: 1)
    result = trade_dispatcher.execute_entry(
        None, "NIFTY50", "NSE:NIFTY26OCT25000CE", "long_call", 25000,
        "CE", 1, 65, 100, authorization=None,
    )
    assert result["executed"] is False
    assert result["mode"] == "rejected"

