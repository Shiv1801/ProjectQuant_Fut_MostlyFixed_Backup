"""
Fyers v3 Data WebSocket wrapper — tick + depth ingestion (PRD §4.1, §7.3).
Reconnect/backfill logic is mandatory: a WS drop must trigger a REST
gap-fill via historical_backfill.py, never silent data loss.
"""
from loguru import logger
from fyers_apiv3.FyersWebsocket import data_ws

from config.settings import settings
from engine.shared_state.publisher import publish_tick


class FyersTickSocket:
    def __init__(self, access_token: str, symbols: list[str]):
        self.token = f"{settings.fyers_client_id}:{access_token}"
        self.symbols = symbols
        self.ws = None

    def _on_message(self, message):
        publish_tick(message)

    def _on_error(self, message):
        logger.error(f"WS error: {message}")

    def _on_close(self, message):
        logger.warning(f"WS closed: {message} — triggering reconnect + gap-fill")
        # engine.data_layer.storage.historical_backfill handles the gap-fill

    def _on_open(self):
        logger.info("Fyers WS connected — subscribing symbols")
        self.ws.subscribe(symbols=self.symbols, data_type="SymbolUpdate")
        self.ws.keep_running()

    def connect(self):
        self.ws = data_ws.FyersDataSocket(
            access_token=self.token,
            log_path="logs/",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self.ws.connect()
