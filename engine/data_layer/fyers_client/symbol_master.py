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


def fetch_nifty_monthly_futures(timeout_seconds: float = 15.0) -> list[dict]:
    """Fetch the public symbol master and return the nearest two contracts."""
    request = Request(
        SYMBOL_MASTER_URL,
        headers={"User-Agent": "ProjectTrade-FuturesDiscovery/1.0"},
    )

    with urlopen(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise RuntimeError(
                f"FYERS symbol master returned HTTP {response.status}"
            )
        payload = response.read()

    master = json.loads(payload.decode("utf-8-sig"))
    contracts = discover_nifty_monthly_futures(master)

    if len(contracts) < 2:
        raise RuntimeError(
            "Could not identify two unexpired standard NIFTY monthly "
            f"futures contracts; found {len(contracts)}."
        )

    return contracts
