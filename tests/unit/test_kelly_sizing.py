"""Unit test for fractional Kelly sizing (PRD §4.5, §3.5-F)."""
from engine.signal_layer.position_sizing.kelly import compute_fractional_kelly


def test_kelly_positive_edge():
    out = compute_fractional_kelly(0.45, 0.27, 0.27, trade_count=30)
    assert out["sized_fraction"] >= 0


def test_kelly_negative_edge_floors_at_zero():
    out = compute_fractional_kelly(0.2, 0.27, 0.27, trade_count=30)
    assert out["full_kelly_fraction"] < 0
    assert out["sized_fraction"] == 0.25
