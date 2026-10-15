"""
Temporary diagnostic script -- NOT production code, not imported by any
production file, not wired into pollers.py/db.py/main.py/gate.py.

Purpose: test the EXISTING historical-data path (get_historical_candles,
the same wrapper engine.data_layer.historical_backfill already uses) for
NSE:NIFTY50-INDEX at 1-minute resolution, and inspect the raw candle
rows for a real, non-zero, varying volume column -- per Fyers' own
community confirmation that index-chart volume (derived from underlying
constituent traded volume) has been available since 1 July 2025.

Does NOT use the Quotes API. Does NOT substitute futures/options/ETF
volume or tick_count. Does NOT modify any production file. Does NOT
add any recurring call -- this is a single one-shot manual script.

Run manually, once, on the machine where today's valid .fyers_token
exists (ideally during/after market hours so there's real intraday
data to inspect):

    python tools/check_fyers_nifty_history_volume.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data_layer.fyers_client.rest_client import get_historical_candles  # existing wrapper, unmodified
from engine.data_layer.historical_backfill import convert_fyers_candles_to_ist  # existing parser, unmodified

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".fyers_token"
SYMBOL = "NSE:NIFTY50-INDEX"
IST = timezone(timedelta(hours=5, minutes=30))


def main():
    if not TOKEN_PATH.exists():
        print(f"ERROR: token file not found at {TOKEN_PATH}")
        sys.exit(1)
    token = TOKEN_PATH.read_text().strip()
    if not token:
        print("ERROR: .fyers_token is empty.")
        sys.exit(1)

    now = datetime.now(IST)
    range_to = now.strftime("%Y-%m-%d")
    range_from = (now - timedelta(days=2)).strftime("%Y-%m-%d")  # small recent sample only

    print(f"SYMBOL: {SYMBOL}")
    print("RESOLUTION: 1 (1-minute)")
    print(f"RANGE: {range_from} to {range_to}")
    print("Requesting via the existing get_historical_candles() wrapper...\n")

    # Exactly one call, same existing wrapper the app's backfill already uses.
    raw = get_historical_candles(token, SYMBOL, "1", range_from, range_to)

    print(f"STATUS (s): {raw.get('s')}")
    print(f"CODE: {raw.get('code')}")
    print(f"MESSAGE: {raw.get('message')}")
    print()

    candles_raw = raw.get("candles", [])
    print(f"Candle rows returned: {len(candles_raw)}")
    print()

    print("RAW RESPONSE (first 5 rows only, full structure preserved):")
    preview = dict(raw)
    if "candles" in preview:
        preview["candles"] = preview["candles"][:5]
    print(json.dumps(preview, indent=2, default=str))
    print()

    if not candles_raw:
        print("No candle rows returned -- cannot verify column structure or volume values.")
        print("(Check STATUS/CODE/MESSAGE above -- may be an auth/token or market-closed issue,")
        print(" not necessarily an absence of volume data.)")
        return

    first_row = candles_raw[0]
    print(f"Columns per row: {len(first_row)}  (expected 6: timestamp, open, high, low, close, volume)")
    print(f"First row raw: {first_row}")
    print()

    # Inspect last 15 rows (most recent intraday activity) for volume behavior
    sample = candles_raw[-15:]
    print("LAST 15 CANDLE ROWS -- [epoch_ts, open, high, low, close, volume]:")
    for row in sample:
        epoch_ts = row[0]
        dt_ist = datetime.fromtimestamp(epoch_ts, tz=timezone.utc).astimezone(IST)
        vol = row[5] if len(row) > 5 else None
        print(f"  {dt_ist.strftime('%Y-%m-%d %H:%M:%S')}  O={row[1]} H={row[2]} L={row[3]} C={row[4]}  VOLUME={vol!r} (type={type(vol).__name__})")

    print()
    volumes = [row[5] for row in candles_raw if len(row) > 5]
    nonzero = [v for v in volumes if v]
    print(f"Total candles with volume data present: {len(volumes)}")
    print(f"Candles with non-zero volume: {len(nonzero)}")
    print(f"Candles with zero/None volume: {len(volumes) - len(nonzero)}")
    if nonzero:
        print(f"Sample non-zero values: {nonzero[:10]}")
        print(f"Distinct non-zero values in last 15 rows: {len({r[5] for r in sample if len(r) > 5 and r[5]})}")
    print()

    # Verify existing parser preserves this volume unchanged
    converted = convert_fyers_candles_to_ist(candles_raw[-5:])
    print("EXISTING PARSER OUTPUT (convert_fyers_candles_to_ist) for last 5 raw rows:")
    for c in converted:
        print(f"  {c}")
    print()
    print("Raw evidence only -- no interpretation of whether this is sufficient for VWAP.")


if __name__ == "__main__":
    main()
