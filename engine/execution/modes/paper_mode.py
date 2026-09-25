"""
PRD §4.5.5 Mode 1 — Paper Trade. Simulated fills must model real-world
friction realistically: bid-ask spread at signal time, slippage model
(§2 cost model), brokerage/STT/charges. A paper account that fills at
mid-price with zero cost is NOT a valid test of the live system.
"""
import uuid
from datetime import datetime

from engine.backtest.cost_model.costs import apply_transaction_costs


class PaperExecutor:
    def __init__(self):
        self.ledger: dict[str, dict] = {}  # in-memory dummy account, own capital/PnL/drawdown

    def open(self, request) -> dict:
        position_id = str(uuid.uuid4())
        simulated_fill_price = self._simulate_fill(request)
        self.ledger[position_id] = {
            "id": position_id, "status": "open", "entry_price": simulated_fill_price,
            "entry_ts": datetime.utcnow(), "request": request,
        }
        return self.ledger[position_id]

    def close(self, position_id: str, exit_reason: str) -> dict:
        pos = self.ledger[position_id]
        exit_price = self._simulate_fill(pos["request"], is_exit=True)
        net = apply_transaction_costs(pos["entry_price"], exit_price, pos["request"].qty)
        pos.update({"status": "closed", "exit_price": exit_price, "exit_reason": exit_reason, "pnl_inr": net})
        return pos

    def update_stops(self, position_id, current_sl, current_tsl_floor):
        self.ledger[position_id]["current_sl"] = current_sl
        self.ledger[position_id]["current_tsl_floor"] = current_tsl_floor
        return self.ledger[position_id]

    def _simulate_fill(self, request, is_exit=False) -> float:
        # Placeholder: pull live bid/ask from shared_state and apply slippage model
        # per config/cost_model.yaml — never fill at raw mid-price.
        raise NotImplementedError("Wire to engine.shared_state + cost_model for realistic sim fills")
