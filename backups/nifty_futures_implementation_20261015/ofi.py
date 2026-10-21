
"""
Group C: Order Flow Imbalance (OFI).

Supports:
1. Existing spot-depth OFI calculation.
2. Discovery of the nearest two unexpired NIFTY monthly futures.
3. Individual futures OFI calculations.
4. Freshness validation and quantity-weighted combined futures OFI.

This module calculates OFI only. Data fetching, polling, shared-state
management, confidence-score integration, and frontend rendering are
handled by their respective application modules.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from numbers import Real
from typing import Any


BUY_PRESSURE_THRESHOLD = 0.2
SELL_PRESSURE_THRESHOLD = -0.2
DEFAULT_MAX_AGE_SECONDS = 15.0
EXPECTED_FUTURES_CONTRACTS = 2

_MONTHLY_NIFTY_FUTURE = re.compile(
    r"^NSE:NIFTY\d{2}"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$",
    re.IGNORECASE,
)


def _result(
    *,
    status: str,
    ofi: float | None,
    total_buy_qty: int | float | None,
    total_sell_qty: int | float | None,
    interpretation: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Build a consistent, JSON-serializable OFI result."""
    return {
        "status": status,
        "ofi": ofi,
        "total_buy_qty": total_buy_qty,
        "total_sell_qty": total_sell_qty,
        "interpretation": interpretation,
        "note": note,
    }


def _validate_quantity(
    value: Any,
    field_name: str,
) -> tuple[int | float | None, str | None]:
    """Validate a depth quantity and return (normalized value, error)."""
    if value is None:
        return None, f"{field_name} is missing"

    if isinstance(value, bool) or not isinstance(value, Real):
        return None, f"{field_name} must be a numeric quantity"

    numeric = float(value)

    if not math.isfinite(numeric):
        return None, f"{field_name} must be finite"

    if numeric < 0:
        return None, f"{field_name} cannot be negative"

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

    Interpretation:
        OFI > +0.2 : buy pressure dominant
        OFI < -0.2 : sell pressure dominant
        Otherwise  : balanced order flow

    Zero total depth returns no_depth, not balanced flow.
    This function does not fetch data or validate timestamps.
    """
    buy, buy_error = _validate_quantity(
        total_buy_qty, "total_buy_qty"
    )
    sell, sell_error = _validate_quantity(
        total_sell_qty, "total_sell_qty"
    )

    if buy_error or sell_error:
        if total_buy_qty is None or total_sell_qty is None:
            status = "missing"
        else:
            status = "invalid"

        note = "; ".join(
            error for error in (buy_error, sell_error) if error
        )

        return _result(
            status=status,
            ofi=None,
            total_buy_qty=buy,
            total_sell_qty=sell,
            interpretation=None,
            note=note or "Invalid depth data",
        )

    assert buy is not None and sell is not None

    total = buy + sell

    if total == 0:
        return _result(
            status="no_depth",
            ofi=None,
            total_buy_qty=0,
            total_sell_qty=0,
            interpretation=None,
            note=(
                "No depth volume available; zero must not be "
                "interpreted as balanced flow."
            ),
        )

    raw_ofi = (buy - sell) / total

    if raw_ofi > BUY_PRESSURE_THRESHOLD:
        interpretation = "buy pressure dominant"
    elif raw_ofi < SELL_PRESSURE_THRESHOLD:
        interpretation = "sell pressure dominant"
    else:
        interpretation = "balanced order flow"

    return _result(
        status="valid",
        ofi=round(raw_ofi, 3),
        total_buy_qty=buy,
        total_sell_qty=sell,
        interpretation=interpretation,
        note=None,
    )


def discover_nifty_monthly_futures(
    master: Any,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Return the nearest two unexpired standard NIFTY monthly futures.

    Accepts the FYERS symbol-master JSON object or a list of records.
    Expiry timestamps from the symbol master are authoritative.
    """
    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    now_epoch = now.timestamp()

    records = master.values() if isinstance(master, dict) else master

    if not isinstance(records, (list, type({}.values()))):
        raise ValueError("Unexpected FYERS symbol-master structure")

    matches: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        symbol = str(record.get("symTicker") or "").strip()

        if not _MONTHLY_NIFTY_FUTURE.fullmatch(symbol):
            continue

        try:
            expiry_epoch = float(record["expiryDate"])
        except (KeyError, TypeError, ValueError):
            continue

        if not math.isfinite(expiry_epoch) or expiry_epoch <= now_epoch:
            continue

        matches.append({
            "symbol": symbol,
            "expiry_epoch": int(expiry_epoch),
            "expiry_utc": datetime.fromtimestamp(
                expiry_epoch, timezone.utc
            ).isoformat(),
            "lot_size": record.get("minLotSize"),
            "tick_size": record.get("tickSize"),
        })

    # Deduplicate symbols and order by expiry.
    unique = {item["symbol"]: item for item in matches}

    return sorted(
        unique.values(),
        key=lambda item: item["expiry_epoch"],
    )[:EXPECTED_FUTURES_CONTRACTS]


def calculate_futures_ofi(
    contracts: list[dict[str, Any]],
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Calculate individual and combined NIFTY futures OFI.

    Each input record should contain:
        symbol
        expiry
        updated_at
        total_buy_qty
        total_sell_qty

    Stale, missing, and invalid snapshots are excluded from the
    combined calculation.

    A one-contract result is marked partial. It is never reported
    as a complete two-contract result.

    The combined value uses aggregate quantities, not the simple
    average of individual OFI values.
    """
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, Real)
        or not math.isfinite(float(max_age_seconds))
        or max_age_seconds < 0
    ):
        raise ValueError("max_age_seconds must be a finite nonnegative number")

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    evaluated: list[dict[str, Any]] = []
    valid_for_combination: list[tuple[dict[str, Any], float, float]] = []

    for item in contracts:
        row: dict[str, Any] = {
            "symbol": item.get("symbol"),
            "expiry": item.get("expiry"),
            "status": "missing",
            "ofi": None,
            "age_seconds": None,
            "total_buy_qty": item.get("total_buy_qty"),
            "total_sell_qty": item.get("total_sell_qty"),
            "interpretation": None,
            "note": None,
        }

        updated_at = item.get("updated_at")

        try:
            updated = datetime.fromisoformat(
                str(updated_at).replace("Z", "+00:00")
            )

            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)

            age = (now - updated).total_seconds()
            row["age_seconds"] = round(age, 2)

        except (TypeError, ValueError):
            row["status"] = "missing_timestamp"
            row["note"] = "Snapshot timestamp is missing or invalid"
            evaluated.append(row)
            continue

        if age < -2:
            row["status"] = "invalid_timestamp"
            row["note"] = "Snapshot timestamp is in the future"
            evaluated.append(row)
            continue

        if age > max_age_seconds:
            row["status"] = "stale"
            row["note"] = "Snapshot exceeds the maximum allowed age"
            evaluated.append(row)
            continue

        result = compute_ofi(
            item.get("total_buy_qty"),
            item.get("total_sell_qty"),
        )
        row.update(result)

        if result["status"] != "valid" or result["ofi"] is None:
            evaluated.append(row)
            continue

        # Use validated quantities from compute_ofi.
        buy = result["total_buy_qty"]
        sell = result["total_sell_qty"]

        if buy is None or sell is None or buy + sell <= 0:
            row["status"] = "no_depth"
            row["ofi"] = None
            row["note"] = "No positive total depth available"
            evaluated.append(row)
            continue

        valid_for_combination.append((row, float(buy), float(sell)))
        evaluated.append(row)

    total_buy = sum(buy for _, buy, _ in valid_for_combination)
    total_sell = sum(sell for _, _, sell in valid_for_combination)
    valid_count = len(valid_for_combination)

    if valid_count == 0 or total_buy + total_sell <= 0:
        combined_ofi = None
        overall_status = "no_valid_data"
        interpretation = None
    else:
        combined_ofi = (total_buy - total_sell) / (total_buy + total_sell)

        if valid_count >= EXPECTED_FUTURES_CONTRACTS:
            overall_status = "valid"
        else:
            overall_status = "partial"

        if combined_ofi > BUY_PRESSURE_THRESHOLD:
            interpretation = "buy pressure dominant"
        elif combined_ofi < SELL_PRESSURE_THRESHOLD:
            interpretation = "sell pressure dominant"
        else:
            interpretation = "balanced order flow"

    return {
        "status": overall_status,
        "combined_ofi": combined_ofi,
        "interpretation": interpretation,
        "valid_contracts": valid_count,
        "expected_contracts": EXPECTED_FUTURES_CONTRACTS,
        "total_buy_qty": total_buy,
        "total_sell_qty": total_sell,
        "contracts": evaluated,
        "max_age_seconds": float(max_age_seconds),
        "diagnostic_only": True,
    }
