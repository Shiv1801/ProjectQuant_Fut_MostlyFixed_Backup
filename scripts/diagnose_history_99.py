"""
Minimal isolated diagnostic for the recurring {'s': 'error', 'code':
-99, 'message': 'Bad request'} error on Fyers' history endpoint. Prints
every detail of exactly ONE request so we can see precisely what's
being sent and what comes back, instead of inferring from a summary.

Run this directly:
    python scripts/diagnose_history_99.py
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from engine.data_layer.fyers_client.auth import get_fyers_model

TOKEN_PATH = Path(".fyers_token")

if not TOKEN_PATH.exists():
    print("No .fyers_token found.")
    sys.exit(1)

access_token = TOKEN_PATH.read_text().strip()
print(f"client_id: {settings.fyers_client_id}")
print(f"token (first 30 chars): {access_token[:30]}...")
print(f"token (last 10 chars): ...{access_token[-10:]}")
print(f"token length: {len(access_token)}")

fyers = get_fyers_model(access_token)

# Try the SIMPLEST possible request: just the last 5 days, matching
# exactly what the app's own "Restore historical data" button sends
# (already known-working pattern), for direct comparison.
end_date = datetime.now()
start_date = end_date - timedelta(days=5)

data = {
    "symbol": "NSE:NIFTY50-INDEX",
    "resolution": "1",
    "date_format": "1",
    "range_from": start_date.strftime("%Y-%m-%d"),
    "range_to": end_date.strftime("%Y-%m-%d"),
    "cont_flag": "1",
}

print(f"\nRequest payload being sent:")
for k, v in data.items():
    print(f"  {k}: {v!r}")

print("\nCalling fyers.history()...")
response = fyers.history(data=data)

print(f"\nFull raw response:")
print(response)
print(f"\nResponse type: {type(response)}")
