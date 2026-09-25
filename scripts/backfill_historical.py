"""One-off / scheduled historical data backfill for the 24-month minimum backtest window (§4.6)."""
from engine.utils.logging_setup import setup_logging

logger = setup_logging()


def main():
    logger.info("Starting historical backfill — target: 24 months, span high-VIX + low-VIX regimes")
    raise NotImplementedError("Loop indices x date range, call rest_client.get_historical, write via TimescaleWriter")


if __name__ == "__main__":
    main()
