"""
Temporary diagnostic script -- NOT production code, not imported anywhere,
makes NO changes to any file or table structure beyond what the existing
candle_volume_enrichment_loop already does (one UPDATE on an existing row).

Purpose: run ONE manual cycle of the exact same logic
candle_volume_enrichment_loop() runs, but with a print statement after
every single step, so we can see exactly where the chain produces 0
instead of real volume -- without waiting for the next minute boundary
and without guessing.

This traces, in order:
  1. raw Fyers history response (status/candle count)
  2. convert_fyers_candles_to_ist() output for the last 3 rows
  3. the exact minute_bucket + volume value about to be passed to
     update_candle_volume()
  4. whether a DB row with that exact minute_bucket already exists
     BEFORE the update (and what its current volume is)
  5. the update_candle_volume() return value (True = a row was matched
     and changed, False = no row matched)
  6. the DB row's volume AFTER the update, read back fresh

Run manually, once, during market hours (today's live candles are what
we need to inspect):

    python tools/trace_volume_enrichment.py
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data_layer.fyers_client.rest_client import get_historical_candles
from engine.data_layer.historical_backfill import convert_fyers_candles_to_ist
from config.db import get_connection, update_candle_volume

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".fyers_token"
SYMBOL = "NSE:NIFTY50-INDEX"
INDEX_ID = "NIFTY50"
IST = timezone(timedelta(hours=5, minutes=30))


def main():
    token = TOKEN_PATH.read_text().strip()
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    print(f"STEP 1: Fyers history request -- symbol={SYMBOL} range={today_str} to {today_str}")
    raw = get_historical_candles(token, SYMBOL, "1", today_str, today_str)
    print(f"  status(s)={raw.get('s')}  code={raw.get('code')}  message={raw.get('message')!r}")
    candles_raw = raw.get("candles", [])
    print(f"  candle rows returned: {len(candles_raw)}")
    if not candles_raw:
        print("  STOP: no candle rows returned -- this is answer (A): Fyers gave nothing for today's range.")
        print("  Check: is the market actually open right now? Is range_from=range_to=today valid for Fyers'")
        print("  history endpoint, or does it require range_from < range_to / a minimum span?")
        return
    print(f"  last raw row: {candles_raw[-1]}")
    print()

    print("STEP 2: convert_fyers_candles_to_ist() on the last 3 raw rows")
    converted = convert_fyers_candles_to_ist(candles_raw)
    last3 = converted[-3:]
    for c in last3:
        print(f"  {c}")
    print()

    conn = get_connection()
    for c in last3:
        mb = c["minute_bucket"]
        vol = c.get("volume")
        print(f"--- Candle minute_bucket={mb!r}  volume_from_fyers={vol!r} (type={type(vol).__name__}) ---")

        print("STEP 4: DB row BEFORE update")
        before = conn.execute(
            "SELECT minute_bucket, volume, tick_count, source FROM candles WHERE index_id = ? AND minute_bucket = ?",
            (INDEX_ID, mb),
        ).fetchone()
        if before is None:
            print(f"  NO ROW EXISTS for index_id={INDEX_ID!r} minute_bucket={mb!r}")
            print("  -> this means update_candle_volume() will match ZERO rows (answer D):")
            print("     the live tick path never created a candle for this exact minute_bucket string.")
        else:
            print(f"  row exists: minute_bucket={before['minute_bucket']!r} volume={before['volume']!r} "
                  f"tick_count={before['tick_count']!r} source={before['source']!r}")
            if before["minute_bucket"] != mb:
                print(f"  *** STRING MISMATCH: DB has {before['minute_bucket']!r} vs Fyers-derived {mb!r} ***")

        print("STEP 5: calling update_candle_volume() exactly as the enrichment loop does")
        if vol is None or vol <= 0:
            print(f"  SKIPPED -- enrichment loop's own guard (vol is not None and vol > 0) would skip this "
                  f"(vol={vol!r}). This is expected/correct behavior for a genuinely zero-volume minute, "
                  f"not a bug -- per spec, zero volume must not be fabricated.")
        else:
            result = update_candle_volume(INDEX_ID, mb, vol)
            print(f"  update_candle_volume() returned: {result}  (True = matched >=1 row, False = matched 0 rows)")

            print("STEP 6: DB row AFTER update, read back fresh")
            after = conn.execute(
                "SELECT minute_bucket, volume FROM candles WHERE index_id = ? AND minute_bucket = ?",
                (INDEX_ID, mb),
            ).fetchone()
            if after is None:
                print("  STILL no row -- confirms answer (D): no matching row to update.")
            else:
                print(f"  after: volume={after['volume']!r}")
                if after["volume"] == vol:
                    print("  MATCH confirmed -- update worked for this candle.")
                elif after["volume"] in (0, 0.0, None):
                    print("  *** UPDATE RAN (rowcount>0) BUT VALUE STILL 0/None -- answer (E) or a second writer")
                    print("      is overwriting this row AFTER the enrichment loop runs (e.g. a later live tick")
                    print("      UPDATE from _upsert_current_minute_candle, which does NOT touch volume, so that")
                    print("      alone can't explain it -- but check for any OTHER code path that also writes")
                    print("      to candles.volume or re-inserts this minute_bucket). ***")
        print()

    conn.close()
    print("Raw evidence only -- no fix applied, no production file modified by this script.")


if __name__ == "__main__":
    main()
