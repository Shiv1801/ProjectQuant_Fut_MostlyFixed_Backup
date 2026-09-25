"""
Tests multiple resolutions (1-min, 5-min, daily) one after another to
isolate whether the -99 "Bad request" is specific to 1-minute
resolution or affects the History endpoint entirely.
"""
from pathlib import Path
from engine.data_layer.fyers_client.rest_client import get_historical_candles

TOKEN_PATH = Path(".fyers_token")
token = TOKEN_PATH.read_text().strip()

symbol = "NSE:NIFTY50-INDEX"
range_from = "2026-09-17"
range_to = "2026-09-23"

for resolution in ["1", "5", "D"]:
    print(f"\n--- Testing resolution={resolution} ---")
    print(f"Requesting: symbol={symbol}, resolution={resolution}, range_from={range_from}, range_to={range_to}")
    result = get_historical_candles(token, symbol, resolution, range_from, range_to)
    print("RAW RESPONSE:")
    print(result)
