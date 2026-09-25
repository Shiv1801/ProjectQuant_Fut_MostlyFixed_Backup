"""Entrypoint: run the full engine loop in Paper mode (default, safe)."""
from engine.utils.logging_setup import setup_logging

logger = setup_logging()


def main():
    logger.info("Starting Project Trade engine in PAPER mode")
    # TODO: wire data_layer WS + confluence_gate loop + risk_engine + PaperExecutor
    raise NotImplementedError("Wire the full engine loop here once modules above are completed")


if __name__ == "__main__":
    main()
