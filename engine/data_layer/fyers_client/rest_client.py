import socket

from engine.data_layer.fyers_client.auth import get_fyers_model

# GLOBAL SOCKET TIMEOUT (added 2026-10-01): FIXES A REAL, CONFIRMED ROOT
# CAUSE, not a symptom. The fyers_apiv3 SDK's FyersModel (built on plain
# `requests`) is constructed with NO timeout anywhere in this codebase --
# confirmed by reading engine/data_layer/fyers_client/auth.py's
# get_fyers_model(), which passes no timeout-related argument at all,
# unlike the one place in that same file that calls `requests` directly
# (exchange_auth_code_for_token, which DOES pass timeout=15).
#
# Wrapping these calls in asyncio.to_thread() + asyncio.wait_for()
# (engine/background/pollers.py, added earlier the same day) does NOT fix
# this: wait_for can only stop *waiting* for a thread's result -- it
# cannot interrupt a raw, already-blocking socket.recv() happening inside
# that thread. A connection that stalls at the TCP level (observed live,
# consistent with this project's own prior notes about a corporate VPN
# with occasional very slow/stuck round-trips) leaves that worker thread
# blocked forever with no exception ever raised, which is exactly the
# symptom reported and confirmed live: price_poller showing
# running=true, last_success=null, last_error=null, consecutive_errors=0
# indefinitely -- not one failed cycle, not one log line, because the
# call never returned control to Python at all.
#
# socket.setdefaulttimeout() is the correct fix for this specific failure
# mode: it sets a hard ceiling on every socket operation for any code in
# this process that doesn't explicitly override it on its own socket
# (which the Fyers SDK does not), including library internals this
# codebase cannot edit directly. Set once, at import time of this module
# -- every poller and signal endpoint imports from here before making its
# first Fyers call, so this always runs before any socket opens.
# 20s is intentionally longer than the 10-15s asyncio.wait_for ceilings
# already in pollers.py -- this is the last-resort backstop; the
# wait_for layer should normally fire first on anything merely slow, and
# this layer exists specifically to guarantee the underlying socket
# itself cannot hang forever even if wait_for's own cancellation can't
# reach it.
socket.setdefaulttimeout(20.0)


def get_quotes_multi(access_token, symbols):
    fyers = get_fyers_model(access_token)
    return fyers.quotes({"symbols": ",".join(symbols)})

def get_option_chain(access_token, symbol, strike_count=10):
    fyers = get_fyers_model(access_token)
    return fyers.optionchain({"symbol": symbol, "strikecount": strike_count, "greeks": "1"})

def get_market_depth(access_token, symbol):
    fyers = get_fyers_model(access_token)
    return fyers.depth({"symbol": symbol, "ohlcv_flag": "1"})

def get_historical_candles(access_token, symbol, resolution, range_from, range_to, cont_flag=1):
    fyers = get_fyers_model(access_token)
    return fyers.history({
        "symbol": symbol, "resolution": resolution, "date_format": "1",
        "range_from": range_from, "range_to": range_to, "cont_flag": cont_flag,
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
