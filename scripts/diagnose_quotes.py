"""
Standalone diagnostic — calls Fyers quotes directly, once, with full
error detail, completely outside our poller/backoff machinery. This
tells us definitively whether the 429 is:
  (a) real rate limiting (would likely succeed once poller is stopped)
  (b) a malformed request (would fail here too, same way)
  (c) something about running as a background asyncio task specifically

Run this while your main uvicorn server is STOPPED, so there's no
poller competing for the rate budget at the same time.
"""
from pathlib import Path
from fyers_apiv3 import fyersModel
from config.settings import settings

TOKEN_PATH = Path(".fyers_token")

if not TOKEN_PATH.exists():
    print("No .fyers_token file found. Log in first.")
    exit(1)

access_token = TOKEN_PATH.read_text().strip()

fyers = fyersModel.FyersModel(
    client_id=settings.fyers_client_id,
    token=access_token,
    is_async=False,
    log_path="",
)

print("Calling fyers.quotes() directly, once...")
print(f"client_id being used: {settings.fyers_client_id}")
print(f"token (first 30 chars): {access_token[:30]}...")
print()

response = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
print("RAW RESPONSE:")
print(response)
