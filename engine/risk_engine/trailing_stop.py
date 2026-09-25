"""
PRD §4.8.2 — Continuous tightening TSL ratchet.
trail_distance(%) = max(floor_pct, base_trail% - (k * profit_above_activation%))
Monotonically non-increasing distance, monotonically non-decreasing locked-in floor.
"""
from dataclasses import dataclass


@dataclass
class TslState:
    active: bool
    peak_profit_pct: float
    trail_distance_pct: float
    locked_in_floor_pct: float


def compute_trail_distance(peak_profit_pct: float, activation_pct: float, base_trail_pct: float,
                            k: float, floor_pct: float) -> float:
    if peak_profit_pct < activation_pct:
        return base_trail_pct  # not yet active
    profit_above_activation = peak_profit_pct - activation_pct
    return max(floor_pct, base_trail_pct - k * profit_above_activation)


def update_tsl(current_peak_profit_pct: float, current_profit_pct: float, activation_pct: float,
               base_trail_pct: float, k: float, floor_pct: float) -> TslState:
    active = current_peak_profit_pct >= activation_pct
    trail_distance = compute_trail_distance(current_peak_profit_pct, activation_pct, base_trail_pct, k, floor_pct)
    locked_in_floor = max(0.0, current_peak_profit_pct - trail_distance) if active else 0.0
    return TslState(active=active, peak_profit_pct=current_peak_profit_pct,
                     trail_distance_pct=trail_distance, locked_in_floor_pct=locked_in_floor)


def check_tsl_exit(current_profit_pct: float, tsl_state: TslState) -> bool:
    """Returns True if current profit has fallen through the locked-in floor -> exit."""
    return tsl_state.active and current_profit_pct <= tsl_state.locked_in_floor_pct
