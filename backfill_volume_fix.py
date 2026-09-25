"""
One-time volume backfill/fix script — standalone, does NOT modify any
existing engine/api files. Fetches real Fyers 1-min candle volume for
the VWAP-usable window (mid-2025 onward, per engine/confluence_gate/
gate.py's documented finding that 2021-2024 has zero real volume data)
and writes it into the newly-added `volume` column on existing rows,
matched by (index_id, minute_bucket) -- an UPDATE, not a fresh INSERT,
since OHLC data for this period already exists in the live DB.

This does not touch insert_historical_candles() or its tick_count
mismatch -- that function is left exactly as-is for now, since it's
used by other live code paths and fixing it is a separate decision.
This script only backfills the volume column directly via its own
UPDATE statements.

Usage:
    python backfill_volume_fix.py
"""
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from engine.background.pollers import SYMBOLS  # noqa
from engine.data_layer.fyers_client.rest_client import get_historical_candles  # noqa

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = Path("data/project_trade.db")
TOKEN_PATH = Path(".fyers_token")


def _read_token() -> str | None:
    # Reads the token file directly rather than importing api.main, so
    # this script is safe to run standalone while uvicorn is already
    # running -- importing api.main would re-execute its startup code
    # (background pollers, port binding) in this second process too.
    if not TOKEN_PATH.exists():
        return None
    return TOKEN_PATH.read_text().strip()
CHUNK_DAYS = 90  # Fyers' per-request limit on 1-min resolution
VWAP_START_DATE = datetime(2025, 6, 1, tzinfo=IST)  # mid-2025 onward, per gate.py's documented finding


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def update_volume_for_index(conn: sqlite3.Connection, index_id: str, token: str, fyers_symbol: str):
    end_date = datetime.now(IST)
    chunk_end = end_date
    total_updated = 0
    chunk_num = 0

    while chunk_end > VWAP_START_DATE:
        chunk_num += 1
        chunk_start = max(VWAP_START_DATE, chunk_end - timedelta(days=CHUNK_DAYS))
        range_from = chunk_start.strftime("%Y-%m-%d")
        range_to = chunk_end.strftime("%Y-%m-%d")

        log(f"  [{index_id}] chunk {chunk_num}: {range_from} to {range_to} ...")
        raw = get_historical_candles(token, fyers_symbol, "1", range_from, range_to)

        if raw.get("s") != "ok":
            log(f"  [{index_id}] chunk {chunk_num} FAILED: {raw}")
            chunk_end = chunk_start
            continue

        candles_raw = raw.get("candles", [])
        updated_this_chunk = 0
        for row in candles_raw:
            epoch_ts, o, h, l, c, v = row
            dt_utc = datetime.fromtimestamp(epoch_ts, tz=timezone.utc)
            dt_ist = dt_utc.astimezone(IST)
            minute_bucket = dt_ist.strftime("%Y-%m-%d %H:%M:00")

            cur = conn.execute(
                "UPDATE candles SET volume = ? WHERE index_id = ? AND minute_bucket = ?",
                (v, index_id, minute_bucket),
            )
            updated_this_chunk += cur.rowcount

        conn.commit()
        total_updated += updated_this_chunk
        log(f"  [{index_id}] chunk {chunk_num}: {updated_this_chunk} rows updated with real volume")

        chunk_end = chunk_start

    log(f"[{index_id}] TOTAL rows updated: {total_updated}")
    return total_updated


def main():
    token = _read_token()
    if not token:
        log("ERROR: No Fyers token found. Log in via the app first (GET /auth/login-url then /auth/exchange).")
        sys.exit(1)

    if not DB_PATH.exists():
        log(f"ERROR: {DB_PATH} not found.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    log("=" * 70)
    log("VOLUME BACKFILL FIX — writing real Fyers volume into the volume column")
    log(f"Window: {VWAP_START_DATE.strftime('%Y-%m-%d')} to {datetime.now(IST).strftime('%Y-%m-%d')}")
    log("=" * 70)

    # NIFTY50 only, per explicit instruction -- BankNifty/FinNifty
    # already excluded from trading and polling per an earlier decision
    # tracked in this project, and skipping them here also avoids
    # burning API calls on data that isn't needed right now.
    index_id = "NIFTY50"
    fyers_symbol = SYMBOLS[index_id]
    log(f"\nStarting {index_id} ({fyers_symbol})...")
    grand_total = update_volume_for_index(conn, index_id, token, fyers_symbol)

    conn.close()
    log(f"\nDONE. Total rows updated: {grand_total}")


if __name__ == "__main__":
    main()
