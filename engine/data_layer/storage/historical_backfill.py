"""
REST-based gap-fill triggered on WS reconnect (PRD §4.1: "Reconnect/backfill
logic: WS drops must trigger REST-based gap-fill, not silent data loss").
"""
from loguru import logger


async def backfill_gap(rest_client, index_id: str, symbol: str, gap_start, gap_end, writer):
    logger.warning(f"Backfilling {symbol} gap {gap_start} -> {gap_end}")
    resp = rest_client.get_historical(symbol, resolution="1", range_from=gap_start, range_to=gap_end)
    candles = resp.get("candles", [])
    for c in candles:
        ts, o, h, l, close, vol = c
        # Historical bars, not raw ticks — flagged in candles table, not ticks table
        await writer.write_candle(index_id, "1", ts, o, h, l, close, vol, tick_count=None)
    logger.info(f"Backfill complete: {len(candles)} candles for {symbol}")
