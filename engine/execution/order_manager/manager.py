"""
PRD F9 — Position & Order Manager. Places, tracks, modifies (SL/TSL
updates), and closes orders. Mode-aware (Paper/Live/Both per §4.5.5).
Delegates to the correct execution strategy in engine/execution/modes/.
"""
from dataclasses import dataclass
from datetime import datetime
from loguru import logger

from engine.execution.modes.paper_mode import PaperExecutor
from engine.execution.modes.live_mode import LiveExecutor
from engine.execution.modes.shadow_mode import ShadowExecutor
from engine.data_layer.storage.timescale_writer import TimescaleWriter


@dataclass
class OrderRequest:
    index_id: str
    strike: float
    expiry: str
    opt_type: str
    qty: int
    signal_id: str


class OrderManager:
    def __init__(self, mode: str, rest_client, writer: TimescaleWriter):
        self.mode = mode  # paper | live | both
        self.writer = writer
        self.executor = self._build_executor(mode, rest_client)

    def _build_executor(self, mode: str, rest_client):
        if mode == "paper":
            return PaperExecutor()
        elif mode == "live":
            return LiveExecutor(rest_client)
        elif mode == "both":
            return ShadowExecutor(rest_client)
        raise ValueError(f"Unknown mode: {mode}")

    def open_position(self, request: OrderRequest) -> dict:
        logger.info(f"Opening position [{self.mode}]: {request}")
        return self.executor.open(request)

    def close_position(self, position_id: str, exit_reason: str) -> dict:
        logger.info(f"Closing position {position_id} reason={exit_reason}")
        result = self.executor.close(position_id, exit_reason)
        # trade_log written exactly once, synchronously, at trade close (F14)
        return result

    def update_sl_tsl(self, position_id: str, current_sl: float, current_tsl_floor: float):
        return self.executor.update_stops(position_id, current_sl, current_tsl_floor)
