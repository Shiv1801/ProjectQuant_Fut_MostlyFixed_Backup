"""
Model E: In-Trade Rejection Detector (PRD §4.8.2b). Re-runs a lightweight
version of the signal stack against an open losing position to decide:
early-exit-on-genuine-rejection vs hold-through-noise vs
liquidity/IV-driven-drop (different response needed).

Critical guardrail from the PRD: this model must itself be backtested
and walk-forward validated (§4.7, §4.8.6) — it must be shown to improve
expectancy (smaller avg loss) without meaningfully hurting win rate.
Do not wire into live SL logic until that validation has run.
"""
from dataclasses import dataclass
from enum import Enum


class RejectionVerdict(str, Enum):
    STRONG_REJECTION = "strong_rejection"       # -> early exit recommended
    AMBIGUOUS_NOISE = "ambiguous_noise"         # -> hold within existing hard SL
    IV_LIQUIDITY_DRIVEN = "iv_liquidity_driven"  # -> flag separately, may hold through SL


@dataclass
class RejectionCheck:
    position_id: str
    delta_direction_ok: bool
    ofi_aligned: bool
    chart_structure_verdict: str
    action_taken: str


def evaluate_losing_position(position: dict, live_greeks: dict, ofi_score: float,
                              chart_structure: str, iv_change_pct: float,
                              spot_change_pct: float) -> RejectionCheck:
    """
    position: current open position dict (direction, entry_delta, etc.)
    live_greeks: current delta/gamma/theta from GreeksEngine
    ofi_score: current OrderFlowImbalance output
    chart_structure: "opposite" | "consolidating" | "confirming" (from §4.3.5-style
                     statistically-defined 1-min structure check, not eyeballing)
    """
    direction = position["direction"]  # "long_call" | "long_put"
    delta_against = (
        (direction == "long_call" and live_greeks["delta"] < position["entry_delta"] * 0.7)
        or (direction == "long_put" and live_greeks["delta"] > position["entry_delta"] * 0.7)
    )
    ofi_against = (direction == "long_call" and ofi_score < -0.15) or (direction == "long_put" and ofi_score > 0.15)
    chart_against = chart_structure == "opposite"

    # IV/liquidity-driven drop: spot barely moved but premium dropped more than
    # theta/IV alone should explain
    iv_liquidity_flag = abs(spot_change_pct) < 0.15 and iv_change_pct < -8.0

    if iv_liquidity_flag:
        verdict = RejectionVerdict.IV_LIQUIDITY_DRIVEN
        action = "flag_for_review_hold_within_sl"
    elif delta_against and ofi_against and chart_against:
        verdict = RejectionVerdict.STRONG_REJECTION
        action = "early_exit_recommended"
    else:
        verdict = RejectionVerdict.AMBIGUOUS_NOISE
        action = "hold_within_existing_sl"

    return RejectionCheck(
        position_id=position["id"],
        delta_direction_ok=not delta_against,
        ofi_aligned=not ofi_against,
        chart_structure_verdict=str(verdict.value),
        action_taken=action,
    )
