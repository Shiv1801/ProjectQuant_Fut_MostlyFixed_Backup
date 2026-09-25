"""Unit tests for the continuous TSL ratchet formula (PRD §4.8.2 worked example)."""
from engine.risk_engine.trailing_stop import compute_trail_distance, update_tsl


def test_trail_distance_at_activation():
    d = compute_trail_distance(peak_profit_pct=15, activation_pct=15, base_trail_pct=12, k=0.15, floor_pct=5)
    assert d == 12


def test_trail_distance_tightens_with_profit():
    d = compute_trail_distance(peak_profit_pct=25, activation_pct=15, base_trail_pct=12, k=0.15, floor_pct=5)
    assert abs(d - 10.5) < 0.01  # matches PRD worked example


def test_trail_distance_hits_floor():
    d = compute_trail_distance(peak_profit_pct=100, activation_pct=15, base_trail_pct=12, k=0.15, floor_pct=5)
    assert d == 5


def test_tsl_not_active_before_activation():
    state = update_tsl(current_peak_profit_pct=10, current_profit_pct=10, activation_pct=15,
                        base_trail_pct=12, k=0.15, floor_pct=5)
    assert state.active is False
