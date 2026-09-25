"""
PRD §4.5.5 Mode 2 — Live Trade. Real orders via Fyers order API. All risk
management and circuit breakers fully active, cannot be bypassed. Requires
explicit separate confirmation to switch into (never auto-switches).
"""
from datetime import datetime


class LiveExecutor:
    def __init__(self, rest_client):
        self.rest_client = rest_client

    def open(self, request) -> dict:
        payload = {
            "symbol": self._build_fyers_symbol(request),
            "qty": request.qty,
            "type": 2,          # market order, per Fyers docs enum — verify at myapi.fyers.in/docsv3
            "side": 1,           # buy
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        response = self.rest_client.place_order(payload)
        return {"id": response.get("id"), "status": "open", "entry_ts": datetime.utcnow(), "raw_response": response}

    def close(self, position_id: str, exit_reason: str) -> dict:
        # Exit order placement via rest_client.place_order with side=-1
        raise NotImplementedError("Wire exit order placement + fill confirmation via order WS")

    def update_stops(self, position_id, current_sl, current_tsl_floor):
        # SL/TSL are enforced by the engine's own tick-driven monitor, not exchange-side
        # GTT orders, per the PRD's "no averaging down, no discretion" hard-exit rule.
        return {"position_id": position_id, "current_sl": current_sl, "current_tsl_floor": current_tsl_floor}

    def _build_fyers_symbol(self, request) -> str:
        # e.g. "NSE:NIFTY24JAN25000CE" — verify exact symbol format at myapi.fyers.in/docsv3
        return f"NSE:{request.index_id}{request.expiry}{int(request.strike)}{request.opt_type}"
