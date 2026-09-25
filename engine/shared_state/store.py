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
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

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

        self.poller_status: dict[str, dict] = {
            "prices": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "option_chain": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
            "depth": {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0},
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
