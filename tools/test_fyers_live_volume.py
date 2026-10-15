"""
READ-ONLY FYERS LIVE VOLUME TEST
---------------------------------
Tests the official FYERS Data WebSocket symbolUpdate feed.

NO database writes.
NO trading.
NO changes to the application.
NO orders.

Run during market hours.
"""

import json
import sys
from pathlib import Path

from fyers_apiv3.FyersWebsocket import data_ws


SYMBOL = "NSE:NIFTY50-INDEX"

TOKEN_PATH = Path(".fyers_token")


def on_message(message):
    print("\n========== FYERS LIVE UPDATE ==========")
    print(json.dumps(message, indent=2, default=str))

    if isinstance(message, dict):
        print("\n========== IMPORTANT FIELDS ==========")

        for key in [
            "symbol",
            "timestamp",
            "ltp",
            "open_price",
            "high_price",
            "low_price",
            "prev_close_price",
            "vol_traded_today",
            "volume",
            "type",
        ]:
            if key in message:
                print(f"{key:22}: {message[key]}")

        # Search recursively for anything containing "vol"
        print("\n========== VOLUME-RELATED FIELDS ==========")

        found = []

        def find_volume(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    current = f"{path}.{k}" if path else k

                    if "vol" in str(k).lower():
                        found.append((current, v))

                    find_volume(v, current)

            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    find_volume(v, f"{path}[{i}]")

        find_volume(message)

        if found:
            for path, value in found:
                print(f"{path}: {value}")
        else:
            print("NO VOLUME FIELD FOUND")

        print("========================================\n")


def on_error(message):
    print("\n========== FYERS ERROR ==========")
    print(json.dumps(message, indent=2, default=str))
    print("=================================\n")


def on_close(message):
    print("\n========== FYERS SOCKET CLOSED ==========")
    print(message)
    print("=========================================\n")


def on_open():
    print("\nConnected to FYERS Data WebSocket.")
    print(f"Subscribing to: {SYMBOL}")
    print("Mode: symbolUpdate")
    print("Waiting for live OHLCV...\n")

    data_type = "SymbolUpdate"

    symbols = [SYMBOL]

    fyers.subscribe(
        symbols=symbols,
        data_type=data_type,
    )

    fyers.keep_running()


if not TOKEN_PATH.exists():
    print(f"ERROR: {TOKEN_PATH} not found.")
    sys.exit(1)

access_token = TOKEN_PATH.read_text().strip()

if not access_token:
    print("ERROR: .fyers_token is empty.")
    sys.exit(1)


# FYERS expects:
# app_id:access_token
# Your existing token file should contain the access token.
#
# Extract app_id from the existing application configuration.
try:
    from config.settings import FYERS_APP_ID

    client_id = FYERS_APP_ID

except Exception:
    print(
        "Could not import FYERS_APP_ID from config.settings.\n"
        "If your project stores the app ID elsewhere, tell me the "
        "existing location and I will adapt the test."
    )
    sys.exit(1)


fyers = data_ws.FyersDataSocket(
    access_token=f"{client_id}:{access_token}",
    log_path="",
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=on_open,
    on_close=on_close,
    on_error=on_error,
    on_message=on_message,
)

print("Connecting to official FYERS Data WebSocket...")
fyers.connect()