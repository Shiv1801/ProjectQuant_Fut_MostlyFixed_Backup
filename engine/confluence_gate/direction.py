"""Shared direction-to-option validation for the production entry path."""

import re


VALID_DIRECTIONS = {"long_call", "long_put"}
_NIFTY_OPTION_SYMBOL = re.compile(r"^NSE:NIFTY\d{2}(?:(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{4,6}|[A-Z]\d{2}\d{4,6})(?:CE|PE)$", re.IGNORECASE)


def expected_option_type(direction: str | None) -> str | None:
    if direction == "long_call":
        return "CE"
    if direction == "long_put":
        return "PE"
    return None


def validate_directional_candidate(direction: str | None, option_type: str | None) -> dict:
    """Return a safe, explicit validation result without changing direction."""
    expected = expected_option_type(direction)
    if expected is None:
        return {
            "valid": False,
            "reason": "invalid_or_missing_quant_direction",
            "expected_option_type": None,
            "actual_option_type": option_type,
        }
    if option_type != expected:
        return {
            "valid": False,
            "reason": "option_type_conflicts_with_quant_direction",
            "expected_option_type": expected,
            "actual_option_type": option_type,
        }
    return {
        "valid": True,
        "reason": None,
        "expected_option_type": expected,
        "actual_option_type": option_type,
    }


def validate_entry_candidate(
    direction: str | None,
    option_type: str | None,
    symbol: str | None = None,
    index_id: str | None = None,
) -> dict:
    """Validate the immutable BUY-only semantics of a new NIFTY entry."""
    result = validate_directional_candidate(direction, option_type)
    if not result["valid"]:
        return result
    if index_id and index_id.upper() != "NIFTY50":
        return {**result, "valid": False, "reason": "unsupported_entry_index"}
    if not isinstance(symbol, str) or not _NIFTY_OPTION_SYMBOL.fullmatch(symbol):
        return {**result, "valid": False, "reason": "unsupported_or_invalid_nifty_option_symbol", "symbol": symbol}
    if not symbol.upper().endswith(option_type.upper()):
        return {**result, "valid": False, "reason": "option_symbol_conflicts_with_option_type", "symbol": symbol}
    return {**result, "transaction_side": "BUY", "symbol": symbol}

