"""
Group C: Order Flow Imbalance (OFI).

Computes normalized buy/sell pressure from Fyers market-depth quantities.

Important semantics:
- status="valid", ofi=0.0 means real balanced flow with positive total depth.
- status="no_depth", ofi=None means both quantities are zero; this is NOT
  classified as balanced flow.
- status="missing" means one or both quantities are unavailable.
- status="invalid" means supplied quantities are malformed or invalid.

This module only calculates OFI. Fetching depth data, tracking freshness, and
rendering dashboard states must be handled by the caller.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Any


BUY_PRESSURE_THRESHOLD = 0.2
SELL_PRESSURE_THRESHOLD = -0.2


def _result(
    *,
    status: str,
    ofi: float | None,
    total_buy_qty: int | float | None,
    total_sell_qty: int | float | None,
    interpretation: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Build a consistent, JSON-serializable result."""
    return {
        "status": status,
        "ofi": ofi,
        "total_buy_qty": total_buy_qty,
        "total_sell_qty": total_sell_qty,
        "interpretation": interpretation,
        "note": note,
    }


def _validate_quantity(value: Any, field_name: str) -> tuple[int | float | None, str | None]:
    """
    Validate a depth quantity.

    Returns (normalized_value, error_message). Booleans are rejected even
    though bool is a subclass of int in Python.
    """
    if value is None:
        return None, f"{field_name} is missing"

    if isinstance(value, bool) or not isinstance(value, Real):
        return None, f"{field_name} must be a numeric quantity"

    numeric = float(value)

    if not math.isfinite(numeric):
        return None, f"{field_name} must be finite"

    if numeric < 0:
        return None, f"{field_name} cannot be negative"

    # Preserve integer quantities as integers in the returned payload.
    if numeric.is_integer():
        return int(numeric), None

    return numeric, None


def compute_ofi(
    total_buy_qty: int | float | None,
    total_sell_qty: int | float | None,
) -> dict[str, Any]:
    """
    Calculate normalized order-flow imbalance in [-1, +1].

    Formula:
        (buy_quantity - sell_quantity) / (buy_quantity + sell_quantity)

    Thresholds retain the existing behavior:
        OFI >  0.2 -> buy pressure dominant
        OFI < -0.2 -> sell pressure dominant
        otherwise  -> balanced order flow

    The function does not fetch market data and cannot determine whether
    otherwise valid quantities are fresh. The caller must track timestamps
    and fetch status separately.
    """
    buy, buy_error = _validate_quantity(total_buy_qty, "total_buy_qty")
    sell, sell_error = _validate_quantity(total_sell_qty, "total_sell_qty")

    if buy_error or sell_error:
        if total_buy_qty is None or total_sell_qty is None:
            status = "missing"
            note = "; ".join(e for e in (buy_error, sell_error) if e)
        else:
            status = "invalid"
            note = "; ".join(e for e in (buy_error, sell_error) if e)

        return _result(
            status=status,
            ofi=None,
            total_buy_qty=buy,
            total_sell_qty=sell,
            interpretation=None,
            note=note or "Invalid depth data",
        )

    # Validation above guarantees both quantities are numeric and nonnegative.
    assert buy is not None and sell is not None

    total = buy + sell

    if total == 0:
        return _result(
            status="no_depth",
            ofi=None,
            total_buy_qty=0,
            total_sell_qty=0,
            interpretation=None,
            note="No depth volume available; zero must not be interpreted as balanced flow.",
        )

    ofi = (buy - sell) / total

    if ofi > BUY_PRESSURE_THRESHOLD:
        interpretation = "buy pressure dominant"
    elif ofi < SELL_PRESSURE_THRESHOLD:
        interpretation = "sell pressure dominant"
    else:
        interpretation = "balanced order flow"

    return _result(
        status="valid",
        ofi=round(ofi, 3),
        total_buy_qty=buy,
        total_sell_qty=sell,
        interpretation=interpretation,
        note=None,
    )
