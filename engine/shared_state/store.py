"""
Shared state — the single in-memory store that background pollers write
to and every /signals/* endpoint reads from. This is what PRD §7.3b
describes: one ingestion point per data type, many readers, no reader
ever triggers its own Fyers call.

Thread-safety note: FastAPI's default asyncio event loop is single-
threaded for our background tasks + request handlers, so a plain dict
is safe here — no separate locking needed as long as we don't introduce
real OS threads elsewhere.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Fyers auth-rejection error codes per official docs' Common API Error
# Codes table — distinct from rate-limit (429) or generic network errors.
# When a poller sees one of these, the token itself is the problem, not
# transient network/rate issues, so the frontend should prompt re-login
# immediately rather than waiting on our own token-age clock.
FYERS_AUTH_ERROR_CODES = {-8, -15, -16, -17}


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def is_fyers_auth_error(error_msg: str) -> bool:
    """Checks whether a poller's last_error string represents a Fyers
    auth/token rejection, by looking for our known error codes."""
    if not error_msg:
        return False
    for code in FYERS_AUTH_ERROR_CODES:
        if f"'code': {code}" in error_msg or f'"code": {code}' in error_msg:
            return True
    return False


class SharedState:
    def __init__(self):
        self.prices: dict[str, dict] = {}          # {"NIFTY50": {...quote...}, ...}
        self.prices_updated_at: str | None = None

        self.option_chains: dict[str, dict] = {}    # {"NIFTY50": {...chain response...}, ...}
        self.option_chains_updated_at: dict[str, str] = {}

        self.depth: dict[str, dict] = {}             # {"NIFTY50": {...depth response...}, ...}
        self.depth_updated_at: dict[str, str] = {}

        # Separate diagnostic store for standard NIFTY monthly futures.
        # This must never overwrite the existing spot-index depth store.
        self.futures_depth: dict[str, dict] = {}
        self.futures_depth_errors: dict[str, str] = {}
        self.futures_contracts: list[dict] = []
        self.futures_contracts_last_known: list[dict] = []
        self.futures_contracts_updated_at: str | None = None
        self.futures_discovery_status = "unavailable"
        self.futures_discovery_error: str | None = None
        self.futures_discovery_updated_at: str | None = None
        self.futures_quotes: dict[str, dict] = {}
        self.futures_candles: dict[str, list[dict]] = {}
        self.futures_data_status: dict[str, dict] = {
            "current_month": {"status": "unavailable", "fields": {}},
            "next_month": {"status": "unavailable", "fields": {}},
        }

        self.poller_status: dict[str, dict] = {
            "prices": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "option_chain": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "depth": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "futures_depth": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "decision_loop": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "position_monitor": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
        }

        # Per-index visibility into the most recent auto-entry cycle —
        # e.g. {"NIFTY50": {"status": "entered_paper", ...}, "BANKNIFTY":
        # {"status": "no_trade", "reason": "capital insufficient"}} —
        # so skips are transparent instead of silent.
        self.auto_entry_last_cycle: dict[str, dict] = {}

    # ---- Prices ----
    def set_prices(self, data: dict):
        self.prices = data
        self.prices_updated_at = now_ist_iso()

    def get_price(self, index_id: str) -> dict | None:
        return self.prices.get(index_id)

    # ---- Option chains ----
    def set_option_chain(self, index_id: str, data: dict):
        self.option_chains[index_id] = data
        self.option_chains_updated_at[index_id] = now_ist_iso()

    def get_option_chain(self, index_id: str) -> dict | None:
        return self.option_chains.get(index_id)

    def get_option_chain_age_seconds(self, index_id: str) -> float | None:
        updated = self.option_chains_updated_at.get(index_id)
        if not updated:
            return None
        updated_dt = datetime.fromisoformat(updated)
        return (datetime.now(IST) - updated_dt).total_seconds()

    # ---- Depth ----
    def set_depth(self, index_id: str, data: dict):
        self.depth[index_id] = data
        self.depth_updated_at[index_id] = now_ist_iso()

    def get_depth(self, index_id: str) -> dict | None:
        return self.depth.get(index_id)

    # ---- Standard NIFTY monthly futures depth (diagnostic only) ----
    def set_futures_contracts(self, contracts: list[dict]):
        selected_at = now_ist_iso()
        previous_symbols = {item.get("symbol") for item in self.futures_contracts}
        selected = [
            {**contract, "selected_at": selected_at, "discovery_status": "available"}
            for contract in contracts
        ]
        selected_symbols = {item.get("symbol") for item in selected}
        for symbol, snapshot in self.futures_depth.items():
            if symbol in previous_symbols - selected_symbols:
                snapshot["quality_status"] = "stale"

        self.futures_contracts = selected
        self.futures_contracts_last_known = list(selected)
        self.futures_contracts_updated_at = selected_at
        self.futures_discovery_status = "available"
        self.futures_discovery_error = None
        self.futures_discovery_updated_at = self.futures_contracts_updated_at
        for contract in selected:
            role = contract["role"]
            self.futures_quotes.pop(role, None)
            self.futures_candles.pop(role, None)
            self.futures_data_status[role] = {
                "status": "unavailable",
                "fields": {},
                "contract_symbol": contract["symbol"],
                "expiry_epoch": contract.get("expiry_epoch"),
                "selected_at": selected_at,
            }

    def mark_futures_discovery_unavailable(self, error: str):
        """Remove live selections while retaining the last selection diagnostically."""
        self.futures_contracts = []
        self.futures_discovery_status = "unavailable"
        self.futures_discovery_error = error
        self.futures_discovery_updated_at = now_ist_iso()
        for status in self.futures_data_status.values():
            if status.get("status") == "available":
                status["status"] = "stale"

    def set_futures_quote(self, role: str, quote: dict):
        self.futures_quotes[role] = dict(quote)
        self.futures_data_status.setdefault(role, {"status": "unavailable", "fields": {}})
        self.futures_data_status[role]["fields"]["quote"] = quote.get("status", "unavailable")
        self.futures_data_status[role]["quote_updated_at"] = quote.get("fetched_at")
        if quote.get("status") == "valid":
            self.futures_data_status[role]["status"] = "available"
            self.futures_data_status[role]["last_success"] = quote.get("fetched_at")
        elif quote.get("status"):
            self.futures_data_status[role]["status"] = quote["status"]

    def set_futures_candles(self, role: str, candles: list[dict], status: str = "available"):
        self.futures_candles[role] = list(candles)
        self.futures_data_status.setdefault(role, {"status": "unavailable", "fields": {}})
        self.futures_data_status[role]["fields"]["candles"] = status
        self.futures_data_status[role]["candle_updated_at"] = now_ist_iso()
        if status == "available":
            self.futures_data_status[role]["last_success"] = self.futures_data_status[role]["candle_updated_at"]

    def get_futures_quote(self, role: str) -> dict | None:
        return self.futures_quotes.get(role)

    def get_futures_candles(self, role: str) -> list[dict]:
        return list(self.futures_candles.get(role, []))

    def set_futures_data_error(self, role: str, field: str, error: str):
        status = self.futures_data_status.setdefault(role, {"status": "unavailable", "fields": {}})
        status["fields"][field] = "error"
        status.setdefault("errors", {})[field] = error
        if status.get("status") == "available":
            status["status"] = "stale"

    def set_futures_field_status(self, role: str, field: str, value: str, updated_at: str | None = None):
        status = self.futures_data_status.setdefault(role, {"status": "unavailable", "fields": {}})
        status["fields"][field] = value
        if updated_at:
            status[f"{field}_updated_at"] = updated_at

    def set_futures_depth(self, symbol: str, data: dict, contract: dict, quality_status: str = "valid", observed_at: str | None = None):
        self.futures_depth[symbol] = {
            "symbol": symbol,
            "role": contract.get("role"),
            "raw": data,
            "expiry_utc": contract.get("expiry_utc"),
            "expiry_epoch": contract.get("expiry_epoch"),
            "lot_size": contract.get("lot_size"),
            "tick_size": contract.get("tick_size"),
            "updated_at": observed_at or now_ist_iso(),
            "quality_status": quality_status,
        }
        self.futures_depth_errors.pop(symbol, None)
        role = contract.get("role")
        if role:
            status = self.futures_data_status.setdefault(role, {"status": "unavailable", "fields": {}})
            status["fields"]["depth"] = quality_status
            status["depth_updated_at"] = self.futures_depth[symbol]["updated_at"]
            if quality_status == "valid":
                status["status"] = "available"
                status["last_success"] = self.futures_depth[symbol]["updated_at"]

    def get_futures_depth(self, symbol: str) -> dict | None:
        return self.futures_depth.get(symbol)

    def set_futures_depth_error(self, symbol: str, error: str):
        self.futures_depth_errors[symbol] = error

    # ---- Poller health ----
    def mark_poller_success(self, poller_name: str):
        s = self.poller_status[poller_name]
        s["running"] = True
        s["last_success"] = now_ist_iso()
        s["consecutive_errors"] = 0
        s["last_error"] = None

    def mark_poller_error(self, poller_name: str, error_msg: str):
        s = self.poller_status[poller_name]
        s["last_error"] = error_msg
        s["consecutive_errors"] += 1

    def has_auth_error(self) -> bool:
        """True if ANY poller's most recent error looks like a Fyers
        token/auth rejection — used to trigger an immediate re-login
        prompt regardless of what our own token-age clock thinks."""
        for name, status in self.poller_status.items():
            if status["last_error"] and is_fyers_auth_error(status["last_error"]):
                return True
        return False


# Single global instance — the actual shared state for the whole process
shared_state = SharedState()
