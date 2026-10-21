"""Unit tests for the AND-gate confluence logic (PRD §4.3.5)."""
from engine.confluence_gate.gate import evaluate_confluence

SAMPLE_CANDLES = [
    {"open": 100, "high": 102, "low": 99, "close": 101},
    {"open": 101, "high": 103, "low": 100, "close": 102},
    {"open": 102, "high": 105, "low": 101, "close": 104},
]


def test_rejects_below_quant_threshold():
    result = evaluate_confluence(
        {"confidence": 0.4, "direction": "long_call"},
        {"verdict": "confirmed", "confirmed": True},
        min_confidence_threshold=0.62,
    )
    assert result["fired"] is False
    assert result["veto_reason"] == "quant_below_threshold"


def test_fires_when_both_legs_agree():
    result = evaluate_confluence(
        {"confidence": 0.75, "direction": "long_call"},
        {"verdict": "confirmed", "confirmed": True},
        min_confidence_threshold=0.62,
    )
    assert result["fired"] is True
    assert result["direction"] == "long_call"
