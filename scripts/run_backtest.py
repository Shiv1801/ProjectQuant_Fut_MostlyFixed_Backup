"""CLI entrypoint for the tick-accurate backtest engine (§4.6)."""
import argparse

from engine.backtest.replay_engine.tick_replay import TickReplayEngine
from engine.utils.logging_setup import setup_logging

logger = setup_logging()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="all")
    parser.add_argument("--window", default="24m", help="e.g. 24m for 24 months, per PRD §4.6 minimum")
    parser.add_argument("--index", default="NIFTY50")
    args = parser.parse_args()

    logger.info(f"Running backtest: models={args.models} window={args.window} index={args.index}")
    raise NotImplementedError("Load tick + option_chain history, run TickReplayEngine.run(...)")


if __name__ == "__main__":
    main()
