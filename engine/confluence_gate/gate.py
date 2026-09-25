"""
Confluence Gate — the actual AND-gate combining Leg 1 (quant confidence)
and Leg 2 (chart structure confirmation), per PRD §4.3.5. No trade fires
from quant score alone, none fires from chart structure alone. Every
evaluation is returned in full, including rejections, for transparency
(matches the PRD's signal_log intent — nothing is a black box).

Leg 3 (VWAP, optional): added per the person's explicit request after
testing showed a real, if modest, directional edge for VWAP-aligned
trades in the one period where VWAP had usable data (mid-2025 onward —
2021-2024 confirmed to have zero real volume in historical data, a
Fyers data-provider limitation, not a strategy flaw). Genuinely
optional per index (defaults OFF), NOT a blanket requirement, since:
  (a) live paper trading has not yet validated this signal at all —
      every number so far came from a backtest later found to carry
      multiple serious bugs (a septillion-percent math error, a
      handful of outlier trades dominating results, and an overnight-
      volatility spike that overpriced modeled premiums by 70%+,
      confirmed against a real trade), and
  (b) unlike Leg 1/Leg 2, VWAP needs real volume data, which the paper-
      trading candle feed may or may not reliably have -- if volume is
      unavailable, this leg is skipped (does not veto), not silently
      treated as a failure, so a real data gap can't quietly stop the
      whole system from trading.
"""


def evaluate_confluence(quant_result: dict, chart_result: dict,
                         min_confidence_threshold: float = 0.5,
                         vwap_result: dict | None = None, require_vwap: bool = False) -> dict:
    quant_confidence = quant_result.get("confidence", 0.0)
    quant_direction = quant_result.get("direction")

    # Leg 1 check — cheap to reject early, no need to even look at chart structure
    if quant_direction is None or quant_confidence < min_confidence_threshold:
        return {
            "fired": False,
            "direction": quant_direction,
            "quant_confidence": quant_confidence,
            "chart_confirmed": False,
            "veto_reason": "quant_below_threshold" if quant_direction else "no_quant_direction",
            "leg1": quant_result,
            "leg2": None,
        }

    # Leg 2 — only evaluated once Leg 1 clears the bar
    chart_verdict = chart_result.get("verdict")
    chart_confirmed = chart_result.get("confirmed", False)

    if chart_verdict == "opposite":
        veto_reason = "chart_veto_opposite_structure"
        fired = False
    elif not chart_confirmed:
        veto_reason = "chart_not_confirmed"
        fired = False
    else:
        veto_reason = None
        fired = True

    # Leg 3 (VWAP) — only evaluated once Legs 1+2 already fired, and
    # only enforced when require_vwap is True for this index. If VWAP
    # data isn't available (vwap_result is None, e.g. no real volume
    # yet today), this leg is SKIPPED, not treated as a failure — a
    # missing signal should never silently veto an otherwise-valid
    # trade, since that would make a real data gap look identical to a
    # genuine directional disagreement.
    vwap_confirmed = None
    if fired and require_vwap:
        if vwap_result is None or vwap_result.get("vwap") is None:
            vwap_confirmed = None  # skipped -- no data, does not veto
        else:
            vwap_signal = "long_call" if vwap_result["price"] > vwap_result["vwap"] else "long_put"
            vwap_confirmed = (vwap_signal == quant_direction)
            if not vwap_confirmed:
                veto_reason = "vwap_disagrees"
                fired = False

    return {
        "fired": fired,
        "direction": quant_direction,
        "quant_confidence": quant_confidence,
        "chart_confirmed": chart_confirmed,
        "vwap_confirmed": vwap_confirmed,
        "veto_reason": veto_reason,
        "leg1": quant_result,
        "leg2": chart_result,
        "leg3": vwap_result,
    }
