"""Fyers option symbol construction/parsing. Verify exact format at https://myapi.fyers.in/docsv3."""
import re


def build_option_symbol(index_id: str, expiry_code: str, strike: int, opt_type: str) -> str:
    """e.g. build_option_symbol("NIFTY", "24JAN", 25000, "CE") -> 'NSE:NIFTY24JAN25000CE'"""
    return f"NSE:{index_id}{expiry_code}{strike}{opt_type}"


def parse_option_symbol(symbol: str) -> dict:
    m = re.match(r"NSE:([A-Z]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)", symbol)
    if not m:
        raise ValueError(f"Cannot parse symbol: {symbol}")
    index_id, expiry_code, strike, opt_type = m.groups()
    return {"index_id": index_id, "expiry_code": expiry_code, "strike": int(strike), "opt_type": opt_type}
