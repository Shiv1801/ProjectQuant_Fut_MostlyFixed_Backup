"""Centralized loguru configuration used by all engine entrypoints."""
import sys
from loguru import logger

from config.settings import settings


def setup_logging():
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level, colorize=True)
    logger.add("logs/engine_{time:YYYY-MM-DD}.log", rotation="1 day", retention="90 days", level="DEBUG")
    return logger
