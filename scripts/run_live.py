"""
Entrypoint: run the engine in LIVE mode. Deliberately separate from
run_paper.py per PRD §4.5.5 — requires explicit go-live gate confirmation,
never auto-switches from paper.
"""
from engine.utils.logging_setup import setup_logging
from engine.execution.mode_switcher import request_mode_switch

logger = setup_logging()


def main():
    confirm = input("Type EXACTLY 'I CONFIRM LIVE TRADING' to proceed: ")
    if confirm != "I CONFIRM LIVE TRADING":
        logger.warning("Live mode start aborted — confirmation not matched")
        return
    logger.critical("Starting Project Trade engine in LIVE mode — real capital at risk")
    # TODO: verify go-live gates cleared (§2) before wiring the loop
    raise NotImplementedError("Wire full live engine loop after paper validation phase (§5, Phase 2-3)")


if __name__ == "__main__":
    main()
