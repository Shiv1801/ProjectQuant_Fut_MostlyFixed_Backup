from engine.data_layer.fyers_client.auth import get_fyers_model

def get_quotes_multi(access_token, symbols):
    fyers = get_fyers_model(access_token)
    return fyers.quotes({"symbols": ",".join(symbols)})

def get_option_chain(access_token, symbol, strike_count=10):
    fyers = get_fyers_model(access_token)
    return fyers.optionchain({"symbol": symbol, "strikecount": strike_count, "greeks": "1"})

def get_market_depth(access_token, symbol):
    fyers = get_fyers_model(access_token)
    return fyers.depth({"symbol": symbol, "ohlcv_flag": "1"})

def get_historical_candles(access_token, symbol, resolution, range_from, range_to):
    fyers = get_fyers_model(access_token)
    return fyers.history({
        "symbol": symbol, "resolution": resolution, "date_format": "1",
        "range_from": range_from, "range_to": range_to, "cont_flag": "1",
    })

def get_funds(access_token: str) -> dict:
    """
    Real account balance/margin. Response schema confirmed via Fyers'
    own real fund_limit structure: a list of rows, each with a 'title'
    (e.g. "Available Balance", "Utilized Amount", "Total Balance") and
    an 'equityAmount'. Different Fyers API versions/accounts may label
    rows slightly differently, so this looks up the row by matching
    title text rather than assuming a fixed index position — more
    robust against exactly this kind of real-world schema variation.
    """
    fyers = get_fyers_model(access_token)
    return fyers.funds()


def extract_available_balance(funds_response: dict) -> float | None:
    """
    Pulls the real, current available-to-trade balance out of Fyers'
    funds() response. Returns None (not a crash, not a guessed
    fallback) if the expected structure isn't present -- callers must
    treat None as "real balance genuinely unavailable right now" and
    fall back to the manually-configured Settings capital, never
    silently substitute a stale or fabricated number.

    Prioritizes "Available Balance" specifically over "Total Balance"
    -- Total Balance includes capital already committed to open
    positions, which is NOT what should drive new position sizing.
    Found and fixed during testing: an earlier version matched
    whichever title came first in the list, which could silently use
    the wrong (larger, inclusive-of-committed-capital) figure.
    """
    if not funds_response or funds_response.get("s") != "ok":
        return None
    fund_limits = funds_response.get("fund_limit", [])
    by_title = {(row.get("title") or "").strip().lower(): row.get("equityAmount") for row in fund_limits}
    if "available balance" in by_title:
        return by_title["available balance"]
    if "total balance" in by_title:
        return by_title["total balance"]
    return None
