"""
PRD §7.4 / §9.2 — Manual override/kill switch. Always-available "flatten
everything now" control, independent of the automated risk engine.
"""
from loguru import logger


def flatten_all_positions(order_manager, positions: list[dict], reason: str = "manual_kill_switch"):
    logger.critical(f"KILL SWITCH ACTIVATED — reason={reason} — flattening {len(positions)} positions")
    results = []
    for pos in positions:
        result = order_manager.close_position(pos["id"], exit_reason="manual_kill_switch")
        results.append(result)
    return results
