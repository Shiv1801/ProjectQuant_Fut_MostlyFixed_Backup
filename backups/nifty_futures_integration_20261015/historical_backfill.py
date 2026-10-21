"""
Historical backfill (PRD §4.1 gap-fill concept, applied here as an
initial-load mechanism). Pulls 1-min candles from Fyers REST history
and converts UTC epoch timestamps to IST minute-buckets matching our
live candle format exactly, so historical + live candles are
interchangeable in the same table.
"""
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def convert_fyers_candles_to_ist(raw_candles: list[list]) -> list[dict]:
    """
    raw_candles: Fyers format, each row = [epoch_ts_utc, open, high, low, close, volume]
    Returns list of dicts with IST-aligned minute_bucket strings, matching
    our live candle schema exactly.
    """
    converted = []
    for row in raw_candles:
        epoch_ts, o, h, l, c, v = row
        dt_utc = datetime.fromtimestamp(epoch_ts, tz=timezone.utc)
        dt_ist = dt_utc.astimezone(IST)
        minute_bucket = dt_ist.strftime("%Y-%m-%d %H:%M:00")
        converted.append({
            "minute_bucket": minute_bucket,
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return converted


def backfill_index_extended(rest_client_fn, access_token: str, fyers_symbol: str, index_id: str,
                             days_back: int, db_insert_fn, chunk_days: int = 90) -> dict:
    """
    Extended backfill for spans longer than Fyers' ~100-day-per-request
    limit on 1-minute resolution (confirmed earlier in this project via
    Fyers' own community documentation). Chains multiple requests
    backward in chunk_days-sized windows -- the same proven pattern
    used successfully in scripts/nifty_mastery_test.py for 3-5 year
    fetches, applied here for a 6-month VOLUME backfill so VWAP has
    real same-day-onward history to work with from the very first
    trading day after this runs, rather than needing to accumulate
    volume live, minute by minute, from a cold start.

    Each chunk goes through the SAME convert_fyers_candles_to_ist +
    db_insert_fn path as the original single-shot backfill_index, so
    volume is captured identically (already confirmed correct earlier
    this session) — this function only adds the chunking needed for a
    genuinely 6-month-long span.
    """
    end_date = datetime.now(IST)
    total_fetched = 0
    total_inserted = 0
    remaining_days = days_back
    chunk_end = end_date
    chunks_run = 0
    errors = []

    while remaining_days > 0:
        chunks_run += 1
        this_chunk = min(chunk_days, remaining_days)
        chunk_start = chunk_end - timedelta(days=this_chunk)

        range_from = chunk_start.strftime("%Y-%m-%d")
        range_to = chunk_end.strftime("%Y-%m-%d")

        raw = rest_client_fn(access_token, fyers_symbol, "1", range_from, range_to)
        if raw.get("s") != "ok":
            errors.append(f"chunk {chunks_run} ({range_from} to {range_to}): {raw}")
        else:
            candles_raw = raw.get("candles", [])
            if candles_raw:
                converted = convert_fyers_candles_to_ist(candles_raw)
                inserted = db_insert_fn(index_id, converted)
                total_fetched += len(converted)
                total_inserted += inserted

        chunk_end = chunk_start
        remaining_days -= this_chunk

    return {
        "status": "ok" if not errors else "partial",
        "index_id": index_id,
        "days_requested": days_back,
        "chunks_run": chunks_run,
        "total_fetched": total_fetched,
        "total_inserted": total_inserted,
        "errors": errors,
    }


def backfill_index(rest_client_fn, access_token: str, fyers_symbol: str, index_id: str,
                    days_back: int, db_insert_fn) -> dict:
    """
    rest_client_fn: engine.data_layer.fyers_client.rest_client.get_historical_candles
    db_insert_fn: config.db.insert_historical_candles
    Fetches 1-min candles for the last `days_back` days and stores them.
    """
    range_to = datetime.now(IST).strftime("%Y-%m-%d")
    range_from = (datetime.now(IST) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    raw = rest_client_fn(access_token, fyers_symbol, "1", range_from, range_to)
    if raw.get("s") != "ok":
        return {"status": "error", "message": f"Fyers history call failed: {raw}"}

    candles_raw = raw.get("candles", [])
    if not candles_raw:
        return {"status": "ok", "index_id": index_id, "fetched": 0, "inserted": 0, "note": "no candles returned (market may be closed for entire range, or symbol has no history)"}

    converted = convert_fyers_candles_to_ist(candles_raw)
    inserted = db_insert_fn(index_id, converted)

    return {
        "status": "ok",
        "index_id": index_id,
        "range_from": range_from,
        "range_to": range_to,
        "fetched": len(converted),
        "inserted": inserted,
    }
