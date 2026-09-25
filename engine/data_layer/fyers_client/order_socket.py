"""
Fyers v3 Order WebSocket — real-time order/position/trade updates.
Used by engine/execution/order_manager to keep positions table in sync
without polling (PRD §7.3b single-write-path rule).
"""
from loguru import logger
from fyers_apiv3.FyersWebsocket import order_ws

from config.settings import settings
from engine.shared_state.publisher import publish_order_update


class FyersOrderSocket:
    def __init__(self, access_token: str):
        self.token = f"{settings.fyers_client_id}:{access_token}"
        self.ws = None

    def _on_order(self, message):
        publish_order_update(message)

    def _on_error(self, message):
        logger.error(f"Order WS error: {message}")

    def connect(self):
        self.ws = order_ws.FyersOrderSocket(
            access_token=self.token,
            log_path="logs/",
            on_connect=lambda: logger.info("Order WS connected"),
            on_close=lambda m: logger.warning(f"Order WS closed: {m}"),
            on_error=self._on_error,
            on_order_update=self._on_order,
        )
        self.ws.connect()
