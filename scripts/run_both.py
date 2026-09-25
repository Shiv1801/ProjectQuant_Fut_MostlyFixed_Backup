"""Entrypoint: run in Both/shadow mode — recommended default ongoing mode post go-live (§4.5.5)."""
from engine.utils.logging_setup import setup_logging

logger = setup_logging()


def main():
    logger.info("Starting Project Trade engine in BOTH (shadow) mode")
    raise NotImplementedError("Wire ShadowExecutor into the main engine loop")


if __name__ == "__main__":
    main()
