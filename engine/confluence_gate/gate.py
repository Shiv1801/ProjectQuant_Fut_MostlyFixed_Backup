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
  (b) unlike Leg 1/Leg 2, VWAP needs real volume data. When enabled,
      unavailable or stale VWAP data fails closed; when disabled it does
      not veto a valid signal.
"""


def evaluate_confluence(quant_result: dict, chart_result: dict,
                         min_confidence_threshold: float = 0.5,
                         vwap_result: dict | None = None, require_vwap: bool = False,
                         require_chart: bool = True,
                         vrp_result: dict | None = None, require_vrp: bool = False,
                         futures_ofi_result: dict | None = None,
                         require_futures_ofi: bool = False,
                         ofi_result: dict | None = None,
                         require_ofi: bool = False) -> dict:
    """
    require_chart: Leg 2 (chart structure) was previously hardcoded as a
    mandatory AND with Leg 1 — there was no way to run on quant
    confidence alone. Per explicit request, Leg 2 is now toggleable the
    same way Leg 3 (VWAP) already was: when require_chart is False,
    chart structure is evaluated for visibility (still returned in the
    response) but never vetoes a trade — only Leg 1 (quant) is
    mandatory. Defaults to True to preserve existing behavior for
    anyone who hasn't explicitly turned it off.
    """
    quant_confidence = quant_result.get("confidence")
    quant_direction = quant_result.get("direction")
    direction_source = quant_result.get("direction_source", "quant_score")

    def alignment(status: str) -> dict:
        return {
            "source": direction_source,
            "status": status,
            "trade_direction": quant_direction,
            "opposite_direction_rejected": True,
        }

    valid_directions = {"long_call", "long_put"}
    import math
    quant_valid = (
        quant_direction in valid_directions
        and isinstance(quant_confidence, (int, float))
        and not isinstance(quant_confidence, bool)
        and math.isfinite(float(quant_confidence))
        and isinstance(min_confidence_threshold, (int, float))
        and math.isfinite(float(min_confidence_threshold))
        and quant_confidence >= min_confidence_threshold
    )
    # Leg 1 check — cheap to reject early, no need to even look at chart structure
    quant_rejection = "quant_below_threshold" if (
        quant_direction in valid_directions
        and isinstance(quant_confidence, (int, float))
        and isinstance(min_confidence_threshold, (int, float))
        and quant_confidence < min_confidence_threshold
    ) else "invalid_or_missing_quant_result"
    if not quant_valid:
        return {
            "fired": False,
            "direction": quant_direction,
            "quant_confidence": quant_confidence,
            "chart_confirmed": False,
            "veto_reason": quant_rejection,
            "leg1": quant_result,
            "leg2": None,
            "directional_alignment": alignment("neutral_blocks_trade" if quant_direction is None else "below_threshold"),
            "quant_min_confidence": min_confidence_threshold,
            "chart_required": require_chart,
            "chart_veto_reason": "quant_rejected",
            "vwap_required": require_vwap,
            "vwap_value": (vwap_result or {}).get("vwap"),
            "vwap_valid": False,
            "futures_ofi_required": require_futures_ofi,
            "futures_ofi_value": (futures_ofi_result or {}).get("combined_ofi"),
            "futures_ofi_valid": False,
            "futures_ofi_contribution": False,
        }

    # Leg 2 — only evaluated once Leg 1 clears the bar. Still computed
    # and returned even when require_chart is False, so the UI always
    # shows what chart structure said -- it just doesn't veto the trade.
    chart_result = chart_result if isinstance(chart_result, dict) else {}
    chart_verdict = chart_result.get("verdict")
    chart_confirmed = chart_result.get("confirmed", False)

    if not require_chart:
        veto_reason = None
        fired = True
    elif chart_verdict == "opposite":
        veto_reason = "chart_veto_opposite_structure"
        fired = False
    elif chart_result.get("fresh") is False or chart_result.get("stale") is True:
        veto_reason = "chart_data_stale"
        fired = False
    elif not chart_confirmed:
        veto_reason = "chart_not_confirmed"
        fired = False
    else:
        veto_reason = None
        fired = True

    vrp_confirmed = None
    if fired and require_vrp:
        if not isinstance(vrp_result, dict) or vrp_result.get("vrp_pct") is None:
            vrp_confirmed = None
            veto_reason = "vrp_unavailable"
            fired = False
        else:
            vrp_confirmed = vrp_result.get("favorable") is True
            if not vrp_confirmed:
                veto_reason = "vrp_unfavorable"
                fired = False

    # Leg 3 (VWAP) is fail-closed when enabled. Missing or stale volume
    # data must not become an accidental bypass of the configured gate.
    vwap_confirmed = None
    if fired and require_vwap:
        if (
            not isinstance(vwap_result, dict)
            or vwap_result.get("available") is not True
            or not isinstance(vwap_result.get("vwap"), (int, float))
            or not isinstance(vwap_result.get("price"), (int, float))
            or vwap_result.get("status") != "FRESH"
        ):
            vwap_confirmed = False
            veto_reason = "vwap_unavailable_or_stale"
            fired = False
        else:
            vwap_signal = "long_call" if vwap_result["price"] > vwap_result["vwap"] else "long_put"
            vwap_confirmed = (vwap_signal == quant_direction)
            if not vwap_confirmed:
                veto_reason = "vwap_disagrees"
                fired = False

    futures_result = futures_ofi_result if isinstance(futures_ofi_result, dict) else {}
    ofi_status = futures_result.get("status")
    futures_value = futures_result.get("combined_ofi")
    futures_valid_rows = [
        row for row in (futures_result.get("contracts") or [])
        if isinstance(row, dict) and row.get("status") == "valid"
    ]
    futures_valid_count = futures_result.get("valid_contracts")
    # A single valid fresh futures contract is a legitimate fallback source.
    # ``partial`` means one of the expected contracts is unavailable, not
    # that the remaining validated snapshot should be discarded.
    ofi_valid = (
        ofi_status in {"ok", "valid"}
        and isinstance(futures_value, (int, float))
        and not isinstance(futures_value, bool)
    ) or (
        ofi_status == "partial"
        and isinstance(futures_value, (int, float))
        and not isinstance(futures_value, bool)
        and isinstance(futures_valid_count, int)
        and futures_valid_count >= 1
        and bool(futures_valid_rows)
    )
    spot_ofi_valid = (
        isinstance(ofi_result, dict)
        and ofi_result.get("status", "valid") == "valid"
        and isinstance(ofi_result.get("ofi"), (int, float))
        and ofi_result.get("fresh") is not False
    )
    quant_ofi_sources = (quant_result.get("ofi_integration") or {}).get("sources", [])
    spot_ofi_valid = spot_ofi_valid or "spot" in quant_ofi_sources or any(
        vote.get("model") == "ofi" for vote in quant_result.get("votes", [])
    )
    if fired and require_ofi and not (ofi_valid or spot_ofi_valid):
        veto_reason = "ofi_unavailable"
        fired = False
    if fired and require_futures_ofi and not ofi_valid:
        veto_reason = "futures_ofi_unavailable"
        fired = False

    return {
        "fired": fired,
        "direction": quant_direction,
        "quant_confidence": quant_confidence,
        "chart_confirmed": chart_confirmed,
        "vwap_confirmed": vwap_confirmed,
        "vrp_confirmed": vrp_confirmed,
        "veto_reason": veto_reason,
        "leg1": quant_result,
        "leg2": chart_result,
        "leg3": vwap_result,
        "vrp": vrp_result,
        "quant_min_confidence": min_confidence_threshold,
        "chart_required": require_chart,
        "chart_veto_reason": veto_reason if not chart_confirmed else None,
        "vwap_required": require_vwap,
        "vwap_value": (vwap_result or {}).get("vwap"),
        "vwap_valid": (vwap_result or {}).get("available") is True and (vwap_result or {}).get("status") == "FRESH",
        "futures_ofi_required": require_futures_ofi,
        "futures_ofi_value": (futures_ofi_result or {}).get("combined_ofi"),
        "futures_ofi_valid": ofi_valid,
        "futures_ofi_contribution": any(v.get("model") == "futures_ofi" for v in quant_result.get("votes", [])),
        "ofi_required": require_ofi,
        "ofi_sources": quant_ofi_sources,
        "ofi_available": ofi_valid or spot_ofi_valid,
        "directional_alignment": alignment("aligned" if fired else "candidate_rejected"),
    }
