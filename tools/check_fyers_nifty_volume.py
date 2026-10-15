"""
Temporary diagnostic script -- NOT production code, not imported by any
production file, not wired into pollers.py/db.py/main.py/gate.py.

Purpose: make EXACTLY ONE quote request for NSE:NIFTY50-INDEX, using the
same existing Fyers auth mechanism and the same existing quote API
wrapper (get_quotes_multi -> engine.data_layer.fyers_client.rest_client)
that the running application already uses -- no new API integration, no
new endpoint, no recurring polling.

Run manually, once, on the machine where today's valid .fyers_token
exists:

    python tools/check_fyers_nifty_volume.py

Prints the complete raw response (token itself is never printed) plus a
recursive search for any key containing "vol", "volume", or "traded",
so the field name is discovered from the real response, not assumed
from documentation. Does not interpret the result -- no claim is made
here about whether VWAP is possible.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data_layer.fyers_client.rest_client import get_quotes_multi  # same existing wrapper, unmodified

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".fyers_token"
SYMBOL = "NSE:NIFTY50-INDEX"


def find_keys_recursive(obj, needles=("vol", "volume", "traded"), path=""):
    """Recursively search obj for any dict key containing one of `needles`
    (case-insensitive substring match). Returns list of (path, value)."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_lower = str(k).lower()
            new_path = f"{path}.{k}" if path else str(k)
            if any(n in key_lower for n in needles):
                hits.append((new_path, v))
            hits.extend(find_keys_recursive(v, needles, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            new_path = f"{path}[{i}]"
            hits.extend(find_keys_recursive(item, needles, new_path))
    return hits


def main():
    if not TOKEN_PATH.exists():
        print(f"ERROR: token file not found at {TOKEN_PATH}")
        print("Run this on the machine where the app's .fyers_token (today's valid token) exists.")
        sys.exit(1)

    token = TOKEN_PATH.read_text().strip()
    if not token:
        print("ERROR: .fyers_token is empty.")
        sys.exit(1)

    print(f"SYMBOL: {SYMBOL}")
    print("Requesting one quote via the existing get_quotes_multi() wrapper...\n")

    # Exactly one call, same existing wrapper/path the app already uses
    # for its price_poller -- no new integration, no recurring call.
    raw = get_quotes_multi(token, [SYMBOL])

    status = raw.get("s")
    code = raw.get("code")
    message = raw.get("message")

    print(f"STATUS (s): {status}")
    print(f"CODE: {code}")
    print(f"MESSAGE: {message}")
    print()
    print("RAW RESPONSE (full, no credentials in this payload to redact):")
    print(json.dumps(raw, indent=2, default=str))
    print()

    # Fyers quotes response shape: {"s": "ok", "d": [{"n": symbol, "v": {...fields...}}]}
    d = raw.get("d")
    if not d:
        print("No 'd' (data) array in response -- cannot inspect per-symbol quote object.")
        print("(This commonly means an auth/token error at the 's'/'code'/'message' level above,")
        print(" not necessarily an absence of a volume field -- see STATUS/CODE/MESSAGE above.)")
        return

    for item in d:
        n = item.get("n")
        v = item.get("v", {})
        print(f"--- Quote object for n={n} ---")
        print("ALL KEYS in this quote's v/data object:")
        for k in v.keys():
            print(f"  {k} = {v[k]!r} (type: {type(v[k]).__name__})")
        print()

    print("VOLUME-RELATED FIELDS (recursive search for keys containing 'vol'/'volume'/'traded'):")
    hits = find_keys_recursive(raw)
    if not hits:
        print("  (none found anywhere in the response)")
    else:
        for path, value in hits:
            print(f"  {path} = {value!r} (type: {type(value).__name__})")

    print()
    print("Raw evidence only -- no interpretation. Re-run this during market hours")
    print("at two different times if you want to see whether any volume-like field changes.")


if __name__ == "__main__":
    main()
