"""Pure validation and normalization helpers for NIFTY futures data."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


FUTURES_ROLES = ("current_month", "next_month")


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            numeric = float(value)
            if math.isfinite(numeric):
                return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def _quote_timestamp(quote: dict[str, Any]) -> str | None:
    for key in ("timestamp", "tt", "time", "lp_time"):
        parsed = _timestamp(quote.get(key))
        if parsed:
            return parsed
    return None


def parse_futures_quotes(
    raw: dict[str, Any],
    contracts: list[dict[str, Any]],
    fetched_at: str,
) -> dict[str, dict[str, Any]]:
    """Normalize one Fyers quotes response without mixing contract roles."""
    by_symbol = {str(item.get("symbol")): item for item in contracts}
    result = {
        item["role"]: {
            "role": item["role"],
            "symbol": item["symbol"],
            "expiry_epoch": item.get("expiry_epoch"),
            "expiry_utc": item.get("expiry_utc"),
            "status": "unavailable",
            "fetched_at": fetched_at,
            "received_at": fetched_at,
            "quote_timestamp": None,
            "error": None,
        }
        for item in contracts
    }

    if not isinstance(raw, dict) or raw.get("s") != "ok":
        error = f"quotes response not ok: {raw}"
        for item in result.values():
            item["error"] = error
        return result

    rows = raw.get("d") or []
    if isinstance(rows, dict):
        rows = [{"n": symbol, "v": value} for symbol, value in rows.items()]

    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("n") or row.get("symbol") or "")
        contract = by_symbol.get(symbol)
        if not contract:
            continue
        quote = row.get("v") if isinstance(row.get("v"), dict) else row
        quote_timestamp = _quote_timestamp(quote)
        output = result[contract["role"]]
        output.update({
            "ltp": _as_float(quote.get("lp")),
            "change": _as_float(quote.get("ch")),
            "change_pct": _as_float(quote.get("chp")),
            "open": _as_float(quote.get("open_price")),
            "high": _as_float(quote.get("high_price")),
            "low": _as_float(quote.get("low_price")),
            "prev_close": _as_float(quote.get("prev_close_price")),
            "quote_timestamp": quote_timestamp,
            "raw": quote,
        })
        if output["ltp"] is None:
            output["status"] = "invalid_quote"
            output["error"] = "quote did not contain a finite last price"
        elif quote_timestamp is None:
            output["status"] = "invalid_timestamp"
            output["error"] = "quote did not contain a valid timestamp"
        else:
            output["status"] = "valid"

    return result


def normalize_futures_candles(
    raw: dict[str, Any],
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize Fyers one-minute candle rows and preserve volume quality."""
    if not isinstance(raw, dict) or raw.get("s") != "ok":
        return []

    by_bucket = {}
    for row in raw.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            epoch = float(row[0])
            if not math.isfinite(epoch):
                continue
            timestamp = datetime.fromtimestamp(epoch, timezone.utc)
        except (TypeError, ValueError, OSError):
            continue

        values = [_as_float(value) for value in row[1:5]]
        if any(value is None for value in values):
            continue
        open_price, high, low, close = values
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            continue
        volume = _as_float(row[5])
        volume_status = "missing" if volume is None else ("zero" if volume == 0 else ("invalid" if volume < 0 else "valid"))
        normalized_row = {
            "role": contract["role"],
            "symbol": contract["symbol"],
            "expiry_epoch": contract.get("expiry_epoch"),
            "expiry_utc": contract.get("expiry_utc"),
            "minute_bucket": timestamp.strftime("%Y-%m-%d %H:%M:00"),
            "candle_timestamp": timestamp.isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_status": volume_status,
            "source": "fyers_history",
        }
        by_bucket[normalized_row["minute_bucket"]] = normalized_row
    return [by_bucket[bucket] for bucket in sorted(by_bucket)]


def build_continuous_futures_candles(rows: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """Stitch contract candles by expiry and back-adjust each rollover.

    The raw contract rows remain unchanged in storage. The returned series uses
    the contract nearest to expiry for each minute, then adds a constant offset
    at each roll so the new contract opens at the previous continuous close.
    """
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("minute_bucket") and row.get("symbol"):
            by_bucket.setdefault(row["minute_bucket"], []).append(row)

    selected = []
    for bucket in sorted(by_bucket):
        candidates = by_bucket[bucket]
        try:
            bucket_date = datetime.fromisoformat(bucket).date()
        except (TypeError, ValueError):
            bucket_date = None
        active = [
            row for row in candidates
            if row.get("expiry_epoch") is not None
            and bucket_date is not None
            and datetime.fromtimestamp(row["expiry_epoch"], timezone.utc).date() >= bucket_date
        ]
        selected.append(min(active or candidates, key=lambda row: (row.get("expiry_epoch") or float("inf"), row["symbol"])))

    continuous = []
    previous_symbol = None
    adjustment = 0.0
    previous_close = None
    for source in selected:
        if previous_symbol is not None and source["symbol"] != previous_symbol:
            adjustment += previous_close - float(source["open"]) - adjustment
        item = dict(source)
        item["contract_symbol"] = source["symbol"]
        item["symbol"] = "NSE:NIFTY_CONTINUOUS_FUT"
        item["continuous_adjustment"] = adjustment
        for field in ("open", "high", "low", "close"):
            item[field] = float(source[field]) + adjustment
        previous_close = item["close"]
        previous_symbol = source["symbol"]
        continuous.append(item)

    return continuous[-limit:] if limit else continuous


def build_live_futures_candle(
    quote: dict[str, Any],
    contract: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Aggregate a genuine quote into the current UTC minute candle.

    The quote's received time determines the minute bucket; its source trade
    timestamp is preserved separately in the quote table. Volume is left
    unavailable because a quote snapshot is not a verified incremental volume
    observation.
    """
    ltp = _as_float(quote.get("ltp"))
    fetched_at = _timestamp(quote.get("fetched_at"))
    if ltp is None or fetched_at is None:
        return None
    observed = datetime.fromisoformat(fetched_at)
    minute_bucket = observed.strftime("%Y-%m-%d %H:%M:00")
    if existing and existing.get("minute_bucket") == minute_bucket:
        open_price = float(existing["open"])
        high = max(float(existing["high"]), ltp)
        low = min(float(existing["low"]), ltp)
        volume = existing.get("volume")
        volume_status = existing.get("volume_status", "unavailable")
    else:
        open_price = high = low = ltp
        volume = None
        volume_status = "unavailable"
    return {
        "role": contract["role"],
        "symbol": contract["symbol"],
        "expiry_epoch": contract.get("expiry_epoch"),
        "minute_bucket": minute_bucket,
        "candle_timestamp": observed.isoformat(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": ltp,
        "volume": volume,
        "volume_status": volume_status,
        "source": "fyers_quote_live",
    }


def normalize_futures_depth(
    raw: dict[str, Any],
    contract: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Normalize depth while distinguishing no depth from an API failure."""
    output = {
        "role": contract["role"],
        "symbol": contract["symbol"],
        "expiry_epoch": contract.get("expiry_epoch"),
        "expiry_utc": contract.get("expiry_utc"),
        "observed_at": observed_at,
        "status": "unavailable",
        "depth_status": "unavailable",
        "total_buy_qty": None,
        "total_sell_qty": None,
        "raw": raw,
        "error": None,
    }
    if not isinstance(raw, dict) or raw.get("s") != "ok":
        output["error"] = f"depth response not ok: {raw}"
        return output

    book = (raw.get("d") or {}).get(contract["symbol"])
    if not isinstance(book, dict):
        output["error"] = "depth response did not contain the requested symbol"
        return output

    provider_timestamp = _quote_timestamp(book)
    if provider_timestamp:
        output["observed_at"] = provider_timestamp
    buy = _as_float(book.get("totalbuyqty"))
    sell = _as_float(book.get("totalsellqty"))
    output["total_buy_qty"] = buy
    output["total_sell_qty"] = sell
    if buy is None or sell is None or buy < 0 or sell < 0:
        output["status"] = "incomplete"
        output["depth_status"] = "incomplete"
        output["error"] = "depth quantities are missing or invalid"
    else:
        output["status"] = "valid"
        output["depth_status"] = "no_depth" if buy + sell == 0 else "valid"
    return output
