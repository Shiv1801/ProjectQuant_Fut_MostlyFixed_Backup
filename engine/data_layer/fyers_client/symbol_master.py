"""Read-only FYERS symbol-master access for NIFTY monthly futures."""
from __future__ import annotations

import json
from urllib.request import Request, urlopen

from engine.signal_layer.momentum.futures_ofi import (
    discover_nifty_monthly_futures,
)

SYMBOL_MASTER_URL = (
    "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"
)


def fetch_nifty_monthly_futures(timeout_seconds: float = 15.0, attempts: int = 3) -> list[dict]:
    """Fetch the public symbol master and return the nearest two contracts."""
    payload = None
    last_error = None
    for _ in range(max(1, attempts)):
        request = Request(
            SYMBOL_MASTER_URL,
            headers={"User-Agent": "ProjectTrade-FuturesDiscovery/1.0"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"FYERS symbol master returned HTTP {response.status}"
                    )
                expected_length = response.headers.get("Content-Length")
                chunks = []
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = b"".join(chunks)
                if expected_length and len(payload) != int(expected_length):
                    raise RuntimeError(
                        "FYERS symbol master response was incomplete: "
                        f"received {len(payload)} of {expected_length} bytes"
                    )
                break
        except Exception as exc:
            last_error = exc
            payload = None

    if payload is None:
        raise RuntimeError(f"Unable to download FYERS symbol master after retries: {last_error}")

    master = json.loads(payload.decode("utf-8-sig"))
    contracts = discover_nifty_monthly_futures(master)

    if len(contracts) < 2:
        raise RuntimeError(
            "Could not identify two unexpired standard NIFTY monthly "
            f"futures contracts; found {len(contracts)}."
        )

    return contracts
