"""Diagnostic calculations for current and next NIFTY monthly futures.

This module does not place trades or modify the existing confluence model.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from engine.signal_layer.momentum.ofi import compute_ofi

IST = timezone.utc  # Expiry timestamps are parsed as absolute instants.

_MONTHLY_NIFTY_FUTURE = re.compile(
    r"^NSE:NIFTY\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$",
    re.IGNORECASE,
)


def discover_nifty_monthly_futures(
    master: Any,
    now: datetime | None = None,
) -> list[dict]:
    """Return the nearest two unexpired standard NIFTY monthly futures.

    Accepts the FYERS symbol-master JSON object or a list of records.
    Expiry timestamps from the master are authoritative.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_epoch = now.timestamp()

    records = master.values() if isinstance(master, dict) else master
    if not isinstance(records, (list, type({}.values()))):
        raise ValueError("Unexpected FYERS symbol-master structure")

    matches = []
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

    # Deduplicate symbols in case the source contains duplicate rows.
    unique = {item["symbol"]: item for item in matches}
    return sorted(
        unique.values(),
        key=lambda item: item["expiry_epoch"],
    )[:2]


def calculate_futures_ofi(
    contracts: list[dict],
    max_age_seconds: float = 15.0,
    now: datetime | None = None,
) -> dict:
    """Calculate per-contract OFI and a quantity-weighted combined value.

    Each input item should contain:
      symbol, expiry, updated_at, total_buy_qty, total_sell_qty

    Stale or invalid contracts are excluded. A one-contract result is
    explicitly marked partial, never as a complete two-contract result.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    evaluated = []
    valid_for_combination = []

    for item in contracts:
        row = {
            "symbol": item.get("symbol"),
            "expiry": item.get("expiry"),
            "status": "missing",
            "ofi": None,
            "age_seconds": None,
            "total_buy_qty": item.get("total_buy_qty"),
            "total_sell_qty": item.get("total_sell_qty"),
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
            evaluated.append(row)
            continue

        if age < -2:
            row["status"] = "invalid_timestamp"
            evaluated.append(row)
            continue
        if age > max_age_seconds:
            row["status"] = "stale"
            evaluated.append(row)
            continue

        result = compute_ofi(
            item.get("total_buy_qty"),
            item.get("total_sell_qty"),
        )
        row.update(result)

        buy = item.get("total_buy_qty")
        sell = item.get("total_sell_qty")
        try:
            buy_num, sell_num = float(buy), float(sell)
        except (TypeError, ValueError):
            buy_num = sell_num = 0.0

        if (
            result.get("ofi") is not None
            and result.get("status") == "valid"
            and math.isfinite(buy_num)
            and math.isfinite(sell_num)
            and buy_num >= 0
            and sell_num >= 0
            and buy_num + sell_num > 0
        ):
            valid_for_combination.append(
                (row, buy_num, sell_num)
            )
        evaluated.append(row)

    total_buy = sum(buy for _, buy, _ in valid_for_combination)
    total_sell = sum(sell for _, _, sell in valid_for_combination)
    valid_count = len(valid_for_combination)

    if valid_count == 0 or total_buy + total_sell <= 0:
        combined = None
        status = "no_valid_data"
    else:
        combined = (total_buy - total_sell) / (total_buy + total_sell)
        status = "valid" if valid_count >= 2 else "partial"

    return {
        "status": status,
        "combined_ofi": combined,
        "valid_contracts": valid_count,
        "expected_contracts": 2,
        "total_buy_qty": total_buy,
        "total_sell_qty": total_sell,
        "contracts": evaluated,
        "max_age_seconds": max_age_seconds,
        "diagnostic_only": True,
    }
