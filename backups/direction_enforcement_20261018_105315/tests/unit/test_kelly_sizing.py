"""Unit test for fractional Kelly sizing (PRD §4.5, §3.5-F)."""
from engine.signal_layer.position_sizing.kelly import FractionalKelly


def test_kelly_positive_edge():
    model = FractionalKelly()
    out = model.compute({
        "live_win_rate": 0.45, "avg_win_pct": 0.27, "avg_loss_pct": 0.27, "kelly_fraction": 0.25,
    })
    assert out.score >= 0


def test_kelly_negative_edge_floors_at_zero():
    model = FractionalKelly()
    out = model.compute({
        "live_win_rate": 0.2, "avg_win_pct": 0.27, "avg_loss_pct": 0.27, "kelly_fraction": 0.25,
    })
    assert out.score == 0.0
