"""Backfill current and next NIFTY futures one-minute history."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.db import init_db, insert_futures_candles
from engine.data_layer.futures_pipeline import normalize_futures_candles
from engine.data_layer.fyers_client.rest_client import get_historical_candles
from engine.data_layer.fyers_client.symbol_master import fetch_nifty_monthly_futures


TOKEN_PATH = Path(".fyers_token")
CHUNK_DAYS = 90


def _read_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError("No .fyers_token file found")
    token = TOKEN_PATH.read_text().strip()
    if not token:
        raise RuntimeError(".fyers_token is empty")
    return token


def backfill(days_back: int = 365) -> dict[str, int]:
    token = _read_token()
    init_db()
    contracts = fetch_nifty_monthly_futures()
    totals = {contract["role"]: 0 for contract in contracts}
    failures = 0
    end = datetime.now()

    for contract in contracts:
        remaining = days_back
        chunk_end = end
        while remaining > 0:
            chunk_days = min(CHUNK_DAYS, remaining)
            chunk_start = chunk_end - timedelta(days=chunk_days)
            raw = get_historical_candles(
                token,
                contract["symbol"],
                "1",
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                0,
            )
            if not isinstance(raw, dict) or raw.get("s") != "ok":
                failures += 1
                print(f"history failed for {contract['symbol']}: {raw}")
                chunk_end = chunk_start
                remaining -= chunk_days
                continue
            candles = normalize_futures_candles(raw, contract)
            totals[contract["role"]] += insert_futures_candles(candles)
            chunk_end = chunk_start
            remaining -= chunk_days

    totals["failures"] = failures
    return totals


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    print(backfill(days))
