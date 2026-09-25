"""
PRD §4.5.5 Mode 3 — Both (parallel shadow mode). Every signal executes
twice: once real (live account), once simulated (paper). Purpose:
continuously measure live-vs-paper divergence to catch cost/slippage
model miscalibration before it costs more capital. Recommended as the
default ongoing mode even after go-live.
"""
from loguru import logger

from engine.execution.modes.live_mode import LiveExecutor
from engine.execution.modes.paper_mode import PaperExecutor


class ShadowExecutor:
    def __init__(self, rest_client):
        self.live = LiveExecutor(rest_client)
        self.paper = PaperExecutor()

    def open(self, request) -> dict:
        live_result = self.live.open(request)
        paper_result = self.paper.open(request)
        self._check_divergence(live_result, paper_result)
        return {"live": live_result, "paper": paper_result}

    def close(self, position_id: str, exit_reason: str) -> dict:
        live_result = self.live.close(position_id, exit_reason)
        paper_result = self.paper.close(position_id, exit_reason)
        self._check_divergence(live_result, paper_result)
        return {"live": live_result, "paper": paper_result}

    def update_stops(self, position_id, current_sl, current_tsl_floor):
        self.live.update_stops(position_id, current_sl, current_tsl_floor)
        return self.paper.update_stops(position_id, current_sl, current_tsl_floor)

    def _check_divergence(self, live_result: dict, paper_result: dict, threshold_pct: float = 5.0):
        """Feeds decay monitoring (§4.7) — if live consistently underperforms paper
        beyond threshold, that's evidence assumptions no longer match live microstructure."""
        live_price = live_result.get("entry_price") or live_result.get("exit_price")
        paper_price = paper_result.get("entry_price") or paper_result.get("exit_price")
        if live_price and paper_price:
            divergence_pct = abs(live_price - paper_price) / paper_price * 100
            if divergence_pct > threshold_pct:
                logger.warning(f"Live/paper divergence {divergence_pct:.2f}% exceeds threshold — flag for cost model review")
