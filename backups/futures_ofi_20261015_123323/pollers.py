"""
Background pollers — run continuously as asyncio tasks from server
startup, independent of any browser/frontend activity. This is the
core engine loop: as long as uvicorn is running, these keep shared_state
fresh, tick history growing, and (once wired) the decision loop
evaluating trades — regardless of whether anyone has the dashboard open.

Rate budget (Fyers: 10/sec, 200/min). Updated 2026-09-25: BankNifty/
FinNifty removed from all pollers (NIFTY50-only app now).
  - prices: base 1 call per 3 sec (all symbols batched into 1 call via
    get_quotes_multi), backs off exponentially (up to 30s) on repeated
    failures — Quotes has its own documented 1 req/sec hard limit
  - option_chain: 1 call per ~2 sec (NIFTY50 only)
  - depth: 1 call per ~2 sec (NIFTY50 only)
  A 1-second interval was tried for option_chain/depth as part of this
  same change and REVERTED: it didn't measurably speed up the app and
  coincided with a slow real Fyers/VPN round-trip that made data look
  very stale (a single call took ~20s -- this codebase's poller calls
  are strictly sequential per-loop, so that was one slow call, not a
  pileup). The genuine, kept improvement from this pass is running
  every Fyers SDK call (which is synchronous/blocking, not real async
  I/O) via asyncio.to_thread(), so one slow call no longer freezes the
  FastAPI event loop / all HTTP responses, as it did before this fix.
"""
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))

# Real NSE 2026 trading holidays (weekday holidays only -- weekends are
# already handled separately by the weekday() check below). Same source
# list used in the frontend's holiday banner, kept in sync manually
# since this is a small, slow-changing list published once a year.
NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
}


def is_within_market_hours(now: datetime | None = None) -> bool:
    """
    The single, shared, REAL market-hours + holiday check used by every
    poller in this file. FIXES A REAL, CONFIRMED GAP: previously only
    account_balance_poller had any hours check at all (added ad hoc,
    not shared), and even that one only checked weekday/weekend, never
    the actual NSE holiday calendar -- meaning EVERY poller, including
    this one, kept hitting Fyers on real trading holidays like Ganesh
    Chaturthi (Sept 14, 2026), when the exchange is genuinely closed.
    """
    if now is None:
        now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    date_str = now.strftime("%Y-%m-%d")
    if date_str in NSE_HOLIDAYS_2026:
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=40, second=0, microsecond=0)
    return start <= now <= end

from engine.shared_state.store import shared_state
from engine.data_layer.fyers_client.rest_client import (
    get_quotes_multi, get_option_chain, get_market_depth, get_funds, extract_available_balance,
)
from config.db import insert_tick

TOKEN_PATH = Path(".fyers_token")

SYMBOLS = {
    # BANKNIFTY/FINNIFTY removed per explicit request (2026-09-25) —
    # this app trades NIFTY50 only. INDIAVIX kept: it feeds the VIX
    # Regime signal, not a tradeable index, so it stays even though
    # only one option-chain index remains.
    "NIFTY50": "NSE:NIFTY50-INDEX",
    "INDIAVIX": "NSE:INDIAVIX-INDEX",
}

CHAIN_INDICES = ["NIFTY50"]  # VIX has no option chain; BankNifty/FinNifty removed


def _read_token() -> str | None:
    if not TOKEN_PATH.exists():
        return None
    return TOKEN_PATH.read_text().strip()


def _parse_quotes_response(raw: dict) -> dict:
    """Same parsing logic previously duplicated across endpoints — now lives in one place."""
    if raw.get("s") != "ok" or not raw.get("d"):
        return {}
    by_symbol = {item["n"]: item["v"] for item in raw["d"]}
    result = {}
    for label, fyers_symbol in SYMBOLS.items():
        q = by_symbol.get(fyers_symbol)
        if not q:
            continue
        result[label] = {
            "ltp": q.get("lp"), "change": q.get("ch"), "change_pct": q.get("chp"),
            "open": q.get("open_price"), "high": q.get("high_price"), "low": q.get("low_price"),
            "prev_close": q.get("prev_close_price"),
        }
    return result


async def price_poller():
    """
    REVERTED to REST polling. The WebSocket attempt (FyersTickSocket)
    failed to start on the real deployment machine because
    fyers_apiv3's data_ws module imports pkg_resources, which is
    genuinely unavailable on this org-managed laptop -- pip install
    setuptools did not fix it (org laptop restrictions likely block or
    redirect the install), and Docker/WSL/Redis are also unavailable
    on this machine (confirmed earlier). Rather than keep chasing an
    environment fix with no reliable path forward, this reverts to the
    REST approach, keeping the real 429-specific backoff fix (a
    genuine improvement found and kept from that attempt) and the
    startup-stagger fix. This is a known-working baseline: it ran
    successfully for the majority of this session before the
    WebSocket attempt.

    NOT tightened further as part of the 2026-09-25 NIFTY-only speed
    pass, honestly: get_quotes_multi already batches every symbol into
    ONE HTTP call regardless of how many are in SYMBOLS, so dropping
    BANKNIFTY/FINNIFTY from SYMBOLS does not reduce call count here at
    all -- there is no freed-up budget on THIS endpoint to spend.
    base_interval stays at 3.0s because Fyers' Quotes endpoint has a
    real, documented 1 req/sec hard limit (independent of the account-
    wide 10/sec ceiling) -- going faster risks 429s, not genuine extra
    freshness.
    """
    import random

    shared_state.poller_status["prices"]["running"] = True
    base_interval = 3.0
    max_backoff = 30.0
    current_backoff = base_interval
    consecutive_429s = 0
    # EXTENDED COOLDOWN (added 2026-10-01): found a real, confirmed gap --
    # the previous max_backoff=30s ceiling was NEVER enough to clear a
    # sustained Fyers-side 429 lock on this specific endpoint. Observed
    # live: 24+ consecutive 429s over ~27 minutes with last_success=null
    # the entire time, retrying every ~30s the whole way -- the quick
    # exponential backoff is correct for a transient burst, but once
    # Fyers has actually throttled this token+endpoint combination for a
    # real cooldown window (not just "you're 1 request too fast"), 30s
    # retries just keep re-triggering it rather than ever recovering.
    # After EXTENDED_COOLDOWN_THRESHOLD consecutive 429s, back off hard
    # to EXTENDED_COOLDOWN_SECONDS instead of the normal 30s ceiling --
    # a few long, spaced-out probes are far more likely to find the
    # throttle actually lifted than many rapid ones that may be
    # perceived by Fyers as prolonging the abuse window.
    EXTENDED_COOLDOWN_THRESHOLD = 10
    EXTENDED_COOLDOWN_SECONDS = 120.0

    # On a fresh server restart, wait briefly before the FIRST request
    # rather than firing immediately -- Fyers' quotes endpoint enforces
    # a hard 1 req/sec limit, and a restart can land its first request
    # too close to whatever the previous process's last request was.
    await asyncio.sleep(2.0)

    while True:
        token = _read_token()
        if not token:
            await asyncio.sleep(2)
            continue
        if not is_within_market_hours():
            shared_state.mark_poller_success("prices")
            await asyncio.sleep(60)
            continue
        try:
            # asyncio.to_thread: get_quotes_multi is a SYNCHRONOUS, blocking
            # network call (fyers_apiv3's SDK uses `requests`, not asyncio) --
            # calling it directly inside this async loop blocks the ENTIRE
            # event loop for the duration of every HTTP round-trip, including
            # incoming web requests to the FastAPI app itself. This was
            # already true before 2026-09-25, but tightening option_chain/
            # depth to 1s intervals (removing their old 0.67s stagger gaps)
            # pushed total blocked-time per second high enough that the
            # server stopped responding to ANY request (confirmed: /docs,
            # a static page with zero custom logic, timed out). Wrapping in
            # to_thread offloads the blocking call to a worker thread so the
            # event loop stays free to serve HTTP requests concurrently.
            # HARD TIMEOUT (added 2026-10-01): found a real, confirmed gap --
            # get_quotes_multi has no configurable timeout of its own (the
            # fyers_apiv3 SDK uses plain `requests` under the hood), so a
            # single slow/hung HTTP round-trip blocks this to_thread call
            # indefinitely. Observed live: after one logged "-99 Bad
            # request" error, last_success froze permanently with
            # consecutive_errors stuck at exactly 1 -- never incrementing,
            # never recovering -- while every OTHER poller in the same
            # process kept ticking normally. That pattern (one isolated
            # thread silently never returning, nothing else affected) is
            # exactly what a hung synchronous call inside asyncio.to_thread
            # looks like: the awaiting coroutine just never wakes up, with
            # no exception and no log line, since nothing ever came back
            # to raise one. wait_for forces a TimeoutError instead of an
            # infinite wait, so the loop can log it and retry next cycle.
            raw = await asyncio.wait_for(
                asyncio.to_thread(get_quotes_multi, token, list(SYMBOLS.values())),
                timeout=10.0,
            )
            parsed = _parse_quotes_response(raw)
            if parsed:
                shared_state.set_prices(parsed)
                for label, quote in parsed.items():
                    insert_tick(label, quote)
                shared_state.mark_poller_success("prices")
                current_backoff = base_interval
                consecutive_429s = 0
            elif raw.get("code") == 429:
                # Fyers' Quotes API rate limit is a hard 1 req/sec
                # (confirmed via Fyers' own community documentation).
                # A 429 means we are ALREADY over that limit right
                # now, so recovery needs a real, immediate cooldown,
                # not the same gradual backoff used for other errors.
                consecutive_429s += 1
                shared_state.mark_poller_error(
                    "prices",
                    f"Rate limited (429) by Fyers Quotes API ({consecutive_429s} consecutive): {raw}"
                    + (f" -- extended {EXTENDED_COOLDOWN_SECONDS:.0f}s cooldown now active"
                       if consecutive_429s >= EXTENDED_COOLDOWN_THRESHOLD else ""),
                )
                if consecutive_429s >= EXTENDED_COOLDOWN_THRESHOLD:
                    current_backoff = EXTENDED_COOLDOWN_SECONDS
                else:
                    current_backoff = min(max(current_backoff * 2.0, 8.0), max_backoff)
            else:
                shared_state.mark_poller_error("prices", f"Empty/invalid response: {raw}")
                current_backoff = min(current_backoff * 1.6, max_backoff)
        except asyncio.TimeoutError:
            # str(TimeoutError()) is empty, which would otherwise log an
            # unhelpful blank error -- named explicitly so this specific,
            # previously-silent failure mode is visible in /pollers/status.
            shared_state.mark_poller_error("prices", "Timed out waiting >10s for Fyers Quotes API response")
            current_backoff = min(current_backoff * 1.6, max_backoff)
            logger.error("price_poller: Fyers Quotes API call timed out after 10s")
        except Exception as e:
            shared_state.mark_poller_error("prices", str(e))
            current_backoff = min(current_backoff * 1.6, max_backoff)
            logger.error(f"price_poller error: {e}")

        jitter = random.uniform(-0.5, 0.5)
        await asyncio.sleep(max(1.0, current_backoff + jitter))


async def option_chain_poller():
    """
    Polls the option chain for NIFTY50 only (BankNifty/FinNifty removed
    2026-09-25). REVERTED the interval-tightening (1s, no stagger) that
    was tried alongside the NIFTY-only cleanup: real testing showed it
    did not actually make the UI faster and, combined with the
    fyers_apiv3 SDK's calls being synchronous/blocking with NO
    configurable timeout, occasional slow round-trips (almost certainly
    the corporate VPN's rotating-IP latency, not this code) stretched a
    single call to 15-20+ seconds -- looking exactly like "everything
    froze" even though each poller cycle is fully sequential (this
    call must finish before the next one starts, so calls can never
    pile up on each other). Back to the original, previously-stable
    ~2s cadence. The asyncio.to_thread() wrap is KEPT -- that part is a
    pure improvement (stops one slow call from freezing the whole
    FastAPI event loop / all HTTP responses), unrelated to and not the
    cause of the reported slowdown.
    """
    from config.trading_config import get_account_config

    shared_state.poller_status["option_chain"]["running"] = True
    while True:
        token = _read_token()
        if not token:
            await asyncio.sleep(2)
            continue
        if not is_within_market_hours():
            shared_state.mark_poller_success("option_chain")
            await asyncio.sleep(60)
            continue
        # Read fresh each cycle so a Settings change takes effect on
        # the next poll without needing a restart — was hardcoded to
        # 10 before; now a real, persisted account-level setting.
        strikes_per_side = get_account_config().get("strikes_tracked_per_side", 10)
        index_id = CHAIN_INDICES[0]  # NIFTY50 only
        fyers_symbol = SYMBOLS[index_id]
        try:
            # See price_poller's to_thread comment -- same root cause, same fix.
            raw = await asyncio.to_thread(get_option_chain, token, fyers_symbol, strike_count=strikes_per_side)
            if raw.get("s") == "ok":
                shared_state.set_option_chain(index_id, raw)
                shared_state.mark_poller_success("option_chain")
            else:
                shared_state.mark_poller_error("option_chain", f"{index_id}: {raw}")
        except Exception as e:
            shared_state.mark_poller_error("option_chain", f"{index_id}: {e}")
            logger.error(f"option_chain_poller error ({index_id}): {e}")
        await asyncio.sleep(2.0)


async def depth_poller():
    """
    Polls market depth for NIFTY50 only. Same stagger-removal rationale
    as option_chain_poller above -- reverted to the original ~2s
    interval for the same reason (see that function's docstring):
    tightening it did not measurably help and coincided with a slow-
    VPN-call producing very stale-looking data. to_thread() kept.
    """
    shared_state.poller_status["depth"]["running"] = True
    while True:
        token = _read_token()
        if not token:
            await asyncio.sleep(2)
            continue
        if not is_within_market_hours():
            shared_state.mark_poller_success("depth")
            await asyncio.sleep(60)
            continue
        index_id = CHAIN_INDICES[0]  # NIFTY50 only
        fyers_symbol = SYMBOLS[index_id]
        try:
            # See price_poller's to_thread comment -- same root cause, same fix.
            raw = await asyncio.to_thread(get_market_depth, token, fyers_symbol)
            if raw.get("s") == "ok":
                shared_state.set_depth(index_id, raw)
                shared_state.mark_poller_success("depth")
            else:
                shared_state.mark_poller_error("depth", f"{index_id}: {raw}")
        except Exception as e:
            shared_state.mark_poller_error("depth", f"{index_id}: {e}")
            logger.error(f"depth_poller error ({index_id}): {e}")
        await asyncio.sleep(2.0)


async def candle_volume_enrichment_loop():
    """
    Added 2026-10-05 for real VWAP (PRD Leg 3), per explicit architecture
    decision: do NOT bolt a new 60-second blind timer onto the existing
    1.5s price_poller / ~2s option_chain/depth loops. Confirmed by live
    diagnostic (tools/check_fyers_nifty_history_volume.py against real
    2026-10-05 data) that:
      - Fyers' QUOTES API gives no usable volume for NSE:NIFTY50-INDEX
        (price_poller's _parse_quotes_response correctly never reads one).
      - Fyers' HISTORY API DOES return genuine per-candle (not cumulative)
        volume for this symbol, confirmed via 375 real candles, 362 with
        non-zero volume.
    So this is a SEPARATE, slower loop, not a change to price_poller.
    It does not touch the 1.5s/2s loops above in any way.

    Tied to the real 1-minute candle boundary (per explicit request) --
    sleeps until a few seconds past each minute rollover, then fetches
    ONLY the just-completed minute's volume (the smallest possible
    range_from/range_to window Fyers' history API accepts -- same day,
    same symbol), so the current, still-forming minute's incomplete
    volume is never used -- avoiding exactly the "contaminated VWAP"
    problem flagged in the architecture review. Reuses the EXISTING
    get_historical_candles() wrapper -- same call signature
    engine.data_layer.historical_backfill.backfill_index already uses --
    no new Fyers endpoint, and this is the only caller of it on a
    recurring cadence (once per minute, not per tick).

    Only enriches the `volume` column of a candle row that the existing
    live tick-based path (_upsert_current_minute_candle) already
    created -- never creates a candle row itself, never touches
    open/high/low/close/tick_count.
    """
    from engine.data_layer.fyers_client.rest_client import get_historical_candles
    from engine.data_layer.historical_backfill import convert_fyers_candles_to_ist
    from config.db import update_candle_volume

    shared_state.poller_status.setdefault(
        "candle_volume_enrichment", {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0}
    )
    shared_state.poller_status["candle_volume_enrichment"]["running"] = True
    index_id = CHAIN_INDICES[0]  # NIFTY50 only -- same single index VWAP applies to
    fyers_symbol = SYMBOLS[index_id]

    while True:
        token = _read_token()
        if not token:
            await asyncio.sleep(5)
            continue
        if not is_within_market_hours():
            shared_state.mark_poller_success("candle_volume_enrichment")
            await asyncio.sleep(60)
            continue

        # Sleep until ~5s past the next minute boundary, so the minute
        # we're about to fetch has fully closed on Fyers' side before we
        # ask for it (avoids fetching a not-yet-finalized current minute).
        now = datetime.now(IST)
        seconds_into_minute = now.second + now.microsecond / 1_000_000
        sleep_for = (65.0 - seconds_into_minute) if seconds_into_minute < 65.0 else (125.0 - seconds_into_minute)
        await asyncio.sleep(max(1.0, sleep_for))

        try:
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            # Smallest possible window -- today only, same symbol, same
            # existing wrapper. Fyers' history endpoint returns the full
            # day's 1-min candles up to "now" for this range; we only use
            # the most recent completed one(s) below.
            raw = await asyncio.to_thread(get_historical_candles, token, fyers_symbol, "1", today_str, today_str)
            if raw.get("s") != "ok":
                shared_state.mark_poller_error("candle_volume_enrichment", f"{index_id}: {raw}")
            else:
                candles_raw = raw.get("candles", [])
                if candles_raw:
                    converted = convert_fyers_candles_to_ist(candles_raw)
                    # Only the last 3 returned minutes are ever enriched per
                    # cycle (cheap, idempotent UPDATE -- handles the rare
                    # case where the previous cycle's last minute hadn't
                    # fully posted volume yet). Never writes into any
                    # minute other than ones the live tick path already
                    # created a row for (update_candle_volume is a no-op
                    # UPDATE on a missing row, never an INSERT).
                    for c in converted[-3:]:
                        vol = c.get("volume")
                        if vol is not None and vol > 0:
                            update_candle_volume(index_id, c["minute_bucket"], vol)
                shared_state.mark_poller_success("candle_volume_enrichment")
        except Exception as e:
            shared_state.mark_poller_error("candle_volume_enrichment", f"{index_id}: {e}")
            logger.error(f"candle_volume_enrichment_loop error ({index_id}): {e}")


def start_candle_volume_enrichment():
    """Separate startable task -- real-VWAP volume enrichment, decoupled
    from the fast 1.5s/2s price/chain/depth loops (see that loop's own
    docstring for the full architecture rationale)."""
    asyncio.create_task(candle_volume_enrichment_loop())
    logger.info("Candle volume enrichment loop started (minute-boundary cadence, NIFTY50 history volume for VWAP)")


async def option_chain_history_loop():
    """
    Added 2026-10-12, per explicit request: ahead of any strike-selector
    scoring upgrade, start capturing REAL historical option-chain data
    (per-strike bid/ask/OI/volume/Greeks) so a future selector change can
    eventually be backtested against genuine market microstructure instead
    of only the Black-Scholes-synthesized approximation in
    engine/backtest/engine_bs.py. Purely additive, read-only with respect
    to everything else:

      - Makes ZERO new Fyers API calls. option_chain_poller() already
        refreshes shared_state's option chain every ~2s for the live UI;
        this loop only READS that already-polled data (same
        _parse_chain_from_shared_state() helper api/main.py's own
        endpoints use) and persists a snapshot once per minute. If that
        poller hasn't run yet or has no data, this loop simply waits --
        it never fetches on its own.
      - Minute-boundary aligned -- one DB write per minute, not per 2s
        tick. The bucket is recalculated after the boundary sleep, and
        cached chain age is checked (maximum 10 seconds) before writing.
      - Writes ONLY to the new option_chain_history table (config/db.py).
        Never touches candles, positions, trade_log, model_scores, or any
        other table. Nothing in the live trading/sizing/execution path
        reads this table -- it exists purely for future backtest/analysis
        use, so this loop cannot affect current trading behavior even if
        it fails entirely (a failure here only means less historical data
        collected, logged via the usual shared_state.mark_poller_error).
    """
    from config.db import insert_option_chain_snapshot

    shared_state.poller_status.setdefault(
        "option_chain_history", {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0}
    )
    shared_state.poller_status["option_chain_history"]["running"] = True
    index_id = CHAIN_INDICES[0]  # NIFTY50 only -- matches every other chain-dependent loop in this file

    while True:
        if not is_within_market_hours():
            shared_state.mark_poller_success("option_chain_history")
            await asyncio.sleep(60)
            continue

        # Sleep until ~2s past the next minute boundary. Do NOT reuse the
        # pre-sleep clock value for the database bucket: the chain is read
        # after this sleep, so the timestamp must be recalculated below.
        now_before_sleep = datetime.now(IST)
        seconds_into_minute = now_before_sleep.second + now_before_sleep.microsecond / 1_000_000
        sleep_for = 62.0 - seconds_into_minute
        await asyncio.sleep(max(1.0, sleep_for))

        try:
            # Recalculate after sleeping so minute_bucket reflects the
            # minute in which this snapshot is actually read.
            snapshot_time = datetime.now(IST)
            if not is_within_market_hours(snapshot_time):
                shared_state.mark_poller_success("option_chain_history")
                continue

            # A cached chain must be recent; otherwise we'd mislabel old
            # market data as a new historical snapshot. The chain poller
            # normally refreshes every ~2s, so 10s allows transient latency
            # without silently storing clearly stale data.
            chain_age = shared_state.get_option_chain_age_seconds(index_id)
            if chain_age is None or chain_age > 10.0:
                status = shared_state.poller_status.setdefault("option_chain_history", {})
                status["last_skipped_reason"] = (
                    "option_chain_age_unavailable" if chain_age is None
                    else f"stale_option_chain_age_seconds={chain_age:.2f}"
                )
                status["skipped_stale_count"] = status.get("skipped_stale_count", 0) + 1
                logger.warning(
                    f"option_chain_history skipped {index_id}: cached chain age="
                    f"{chain_age!r}s (maximum 10s)"
                )
                shared_state.mark_poller_error("option_chain_history", status["last_skipped_reason"])
                continue

            # Local import avoids circular import at module load -- same
            # established pattern as the auto_entry_loop import above.
            from api.main import _parse_chain_from_shared_state, _parse_expiry_epoch

            parsed = _parse_chain_from_shared_state(index_id)
            if parsed is None or not parsed.get("strikes"):
                shared_state.mark_poller_error("option_chain_history", "fresh_chain_has_no_parsed_strikes")
                continue

            expiry_dts = [d for d in (_parse_expiry_epoch(e.get("expiry")) for e in parsed.get("expiry_data", [])) if d is not None]
            days_to_expiry = round((min(expiry_dts) - datetime.utcnow()).total_seconds() / 86400, 2) if expiry_dts else None

            minute_bucket = snapshot_time.strftime("%Y-%m-%d %H:%M:00")
            rows = []
            for strike_row in parsed["strikes"]:
                for side in ("CE", "PE"):
                    option_data = strike_row.get(side)
                    if not option_data:
                        continue
                    rows.append({
                        "strike": strike_row["strike"], "option_type": side,
                        "symbol": option_data.get("symbol"), "ltp": option_data.get("ltp"),
                        "bid": option_data.get("bid"), "ask": option_data.get("ask"),
                        "oi": option_data.get("oi"), "oi_change": option_data.get("oi_change"),
                        "volume": option_data.get("volume"), "iv": option_data.get("iv"),
                        "delta": option_data.get("delta"), "gamma": option_data.get("gamma"),
                        "theta": option_data.get("theta"), "vega": option_data.get("vega"),
                    })

            if not rows:
                shared_state.mark_poller_error("option_chain_history", "parsed_chain_contained_zero_option_rows")
                continue

            insert_option_chain_snapshot(index_id, minute_bucket, parsed.get("spot"), days_to_expiry, rows)
            status = shared_state.poller_status.setdefault("option_chain_history", {})
            status["last_snapshot_bucket"] = minute_bucket
            status["last_snapshot_rows"] = len(rows)
            status["last_snapshot_strikes"] = len({row["strike"] for row in rows})
            status["last_snapshot_chain_age_seconds"] = round(float(chain_age), 2)
            status["last_skipped_reason"] = None
            shared_state.mark_poller_success("option_chain_history")
        except Exception as e:
            shared_state.mark_poller_error("option_chain_history", f"{index_id}: {e}")
            logger.error(f"option_chain_history_loop error ({index_id}): {e}")


def start_option_chain_history_loop():
    """Separate startable task -- historical option-chain capture for
    future backtest validation, decoupled from every other poller (see
    that loop's own docstring for the full architecture rationale)."""
    asyncio.create_task(option_chain_history_loop())
    logger.info("Option chain history loop started (minute-boundary cadence, NIFTY50 chain snapshot capture for future backtesting)")


def start_background_pollers():
    """Called once from FastAPI startup — schedules all pollers as
    fire-and-forget asyncio tasks that run for the life of the process."""
    logger.info("POLLERS.PY VERSION CHECK: holiday-aware is_within_market_hours() is ACTIVE in this running process.")
    asyncio.create_task(price_poller())
    asyncio.create_task(option_chain_poller())
    asyncio.create_task(depth_poller())
    asyncio.create_task(account_balance_poller())
    start_candle_volume_enrichment()
    logger.info("Background pollers started: prices (NIFTY50+VIX, ~3s), option_chain (NIFTY50 only, ~2s), depth (NIFTY50 only, ~2s), account_balance (~30s), candle_volume_enrichment (minute-boundary)")


async def account_balance_poller():
    """
    Periodically pulls REAL Fyers account balance/margin and stores it
    in the persisted account config, so position sizing can use actual
    available capital instead of a manually-typed Settings number.
    Uses the shared, holiday-aware is_within_market_hours() check
    defined at module level (previously had its own local, weekday-
    only check that didn't know about NSE holidays -- fixed to match
    every other poller in this file).
    """
    from config.trading_config import update_account_config

    shared_state.poller_status.setdefault("account_balance", {"running": False, "last_success": None, "last_error": None, "consecutive_errors": 0})
    shared_state.poller_status["account_balance"]["running"] = True
    while True:
        token = _read_token()
        if not token:
            await asyncio.sleep(5)
            continue
        if not is_within_market_hours():
            shared_state.mark_poller_success("account_balance")
            await asyncio.sleep(120)
            continue
        try:
            # See price_poller's to_thread comment -- same root cause, same fix.
            raw = await asyncio.to_thread(get_funds, token)
            balance = extract_available_balance(raw)
            if balance is not None:
                update_account_config({
                    "real_available_balance": balance,
                    "real_balance_last_updated": datetime.now(IST).isoformat(),
                })
                shared_state.mark_poller_success("account_balance")
            else:
                shared_state.mark_poller_error("account_balance", f"Could not extract balance from: {raw}")
        except Exception as e:
            shared_state.mark_poller_error("account_balance", str(e))
            logger.error(f"account_balance_poller error: {e}")
        await asyncio.sleep(30)  # balance doesn't need second-by-second freshness


async def position_monitor_loop():
    """
    Runs continuously, checking all open positions against SL/TSL/time-
    stop/strike-scope. TIGHTENED from ~5s to sub-second (0.5s) per
    explicit request (2026-09-28): with live_trading_master_enabled ON
    and real capital on the line, exit timeliness (hard SL, TSL, strike
    moving out of tracked range) is the correct place to spend speed
    budget, unlike the entry side (price/option_chain/depth polling),
    which was deliberately left at its existing safer interval -- see
    that reasoning in price_poller/option_chain_poller above; the two
    are independent settings and this change does not touch those.

    HONEST LIMITATION: this loop reads price/strike data from
    shared_state, which option_chain_poller/depth_poller only refresh
    every ~2s -- checking faster than that does NOT get fresher
    underlying market data. What it DOES buy: the moment a new price
    snapshot lands, this loop reacts to it within ~0.5s instead of up
    to ~5s, so the real gain is in REACTION latency to already-current
    data, not in seeing the market any sooner than before.

    REAL BUG FIXED ALONGSIDE this speed change, not just a side note:
    monitor_and_close_positions() -> execute_exit() -> place_live_exit_order()
    calls fyers.place_order(), which is a SYNCHRONOUS, BLOCKING network
    call (same root cause as the 2026-09-25 pollers.py freeze fix) --
    it was NOT wrapped in asyncio.to_thread() here. Tightening this
    loop's interval WITHOUT fixing that first would have reintroduced
    the exact same event-loop-freeze bug, except now potentially
    stalling the whole server while a REAL exit order is in flight,
    which is worse than a stale price. Now wrapped in to_thread().

    Each position is checked against ITS OWN index's risk settings.
    """
    from engine.execution.paper_executor import monitor_and_close_positions
    from api.main import _parse_chain_from_shared_state  # local import avoids circular import at module load
    from config.trading_config import get_index_config

    def get_index_risk_params(index_id: str) -> dict:
        cfg = get_index_config(index_id)
        return cfg["risk_params"] if cfg else {}

    shared_state.poller_status["position_monitor"]["running"] = True
    while True:
        try:
            token = _read_token()
            # to_thread: monitor_and_close_positions can call a blocking
            # fyers.place_order() when closing a live position -- see
            # docstring above. Without this, a slow order-placement call
            # would freeze the whole FastAPI event loop, same as the
            # 2026-09-25 pollers bug.
            actions = await asyncio.to_thread(
                monitor_and_close_positions, _parse_chain_from_shared_state, get_index_risk_params, access_token=token,
            )
            # Mark success on every clean cycle, not only when an action
            # happened — otherwise a healthy loop with nothing to do
            # (no open positions, or open positions not yet hitting any
            # exit condition) looks identical to a genuinely stuck loop,
            # since last_success would never update either way. This was
            # a real bug: it made "existing trades not exiting" and "the
            # loop silently died" indistinguishable from the status endpoint.
            shared_state.mark_poller_success("position_monitor")
            if actions:
                logger.info(f"position_monitor_loop: {len(actions)} position(s) evaluated, actions: {actions}")
        except Exception as e:
            shared_state.mark_poller_error("position_monitor", str(e))
            logger.error(f"position_monitor_loop error: {e}")
        await asyncio.sleep(0.5)


def start_position_monitor():
    """Separate from start_background_pollers so the two concerns (data
    ingestion vs. position management) stay independently startable/testable."""
    asyncio.create_task(position_monitor_loop())
    logger.info("Position monitor loop started (0.5s interval, sub-second -- exit safety on real positions)")


async def auto_entry_loop():
    """
    Autonomous decision loop — evaluates the confluence gate for each
    index INDEPENDENTLY, using that index's OWN risk settings, mode,
    and capital allocation. An index only trades when ITS OWN settings'
    conditions are met — different indices can have entirely different
    SL/TSL/confidence thresholds, and an index set to "off" is skipped
    entirely regardless of what other indices are doing.

    Design (per explicit discussion):
      - PAPER entry is ALWAYS attempted when an index's gate fires and
        its mode is paper/live/both (not off) — paper tracking is the
        permanent baseline record and is never skipped.
      - LIVE order placement is additive, gated on that index's OWN
        mode being "live" or "both" — NOT YET IMPLEMENTED, honestly
        reported as such rather than silently doing nothing.
      - Each index's capital comes from its own allocation % of total
        capital (get_index_capital), not a shared pool — no artificial
        minimum enforced; 0 lots is a valid, expected outcome for an
        index whose allocated capital can't afford its premium.
      - "off" mode skips the index completely — no gate evaluation, no
        sizing, no entry attempt of any kind.

    Safety behaviors (unchanged):
      - Auto-entry itself disabled by default at the account level.
      - Skips an index if it already has an open position (no pyramiding).
      - Respects account-level circuit breakers before sizing.
    """
    from config.trading_config import get_account_config, get_index_config, get_index_capital, INDICES
    from config.db import get_open_positions, get_trade_log
    from engine.execution.paper_executor import execute_paper_entry
    from engine.execution.trade_dispatcher import execute_entry
    from engine.risk_engine.circuit_breakers import evaluate_circuit_breakers
    # Strategy entry-gate layer (liquidity/spread/volatility) removed 2026-10-07,
    # per explicit request -- confluence + sizing + the risk/circuit-breaker
    # checks below are now the sole authority on execution.

    shared_state.poller_status["decision_loop"]["running"] = True
    while True:
        account_config = get_account_config()
        if not account_config.get("auto_entry_enabled"):
            await asyncio.sleep(account_config.get("auto_entry_poll_seconds", 10))
            continue

        try:
            from api.main import position_size_signal  # local import avoids circular import at module load

            open_positions = get_open_positions()
            open_index_ids = {p["index_id"] for p in open_positions}

            recent_trades = get_trade_log(limit=10)
            consecutive_losses = 0
            for t in recent_trades:
                if t["pnl"] <= 0:
                    consecutive_losses += 1
                else:
                    break

            breaker_check = evaluate_circuit_breakers(
                daily_pnl_pct=0.0,  # TODO: wire real daily PnL once available capital tracking exists
                consecutive_losses=consecutive_losses,
                open_position_count=len(open_positions),
                daily_loss_limit_pct=account_config["daily_loss_limit_pct"],
                consecutive_loss_throttle=account_config["consecutive_loss_throttle"],
                consecutive_loss_size_multiplier=account_config["consecutive_loss_size_multiplier"],
                max_concurrent_positions=account_config["max_concurrent_positions"],
            )

            index_status = {}  # per-index visibility into why a trade did/didn't happen this cycle

            if breaker_check["breaker_tripped"]:
                logger.info(f"AUTO-ENTRY RISK VETO: circuit breaker tripped — {breaker_check['breaker_reason']} (existing risk control, not an execution error)")
                shared_state.mark_poller_error("decision_loop", f"circuit breaker tripped: {breaker_check['breaker_reason']}")
            else:
                for index_id in INDICES:
                    index_cfg = get_index_config(index_id)
                    mode = index_cfg["mode"]

                    if mode == "off":
                        index_status[index_id] = {"status": "off", "reason": "trading mode set to Off for this index"}
                        continue

                    if index_id in open_index_ids:
                        logger.info(f"AUTO-ENTRY RISK VETO: index={index_id} — position already open (no-pyramiding, existing risk control)")
                        index_status[index_id] = {"status": "skipped", "reason": "position already open"}
                        continue

                    risk_params = index_cfg["risk_params"]
                    index_capital = get_index_capital(index_id)

                    sizing = await position_size_signal(
                        index_id, available_capital=index_capital,
                        risk_per_trade_pct=risk_params["risk_per_trade_pct"],
                        hard_sl_pct=risk_params["hard_sl_pct"],
                        min_confidence=risk_params["min_confluence_confidence"],
                    )

                    if sizing.get("lots", 0) < 1:
                        # LIVE MODE OVERRIDE, per explicit request: sizing
                        # correctly computed 0 lots (e.g. real capital is
                        # genuinely 0) -- normally this means no trade
                        # attempt at all, so a real capital shortfall
                        # would NEVER surface as an actual Fyers rejection.
                        # For live/both mode, if the gate DID fire and a
                        # real strike exists (sizing just couldn't afford
                        # any lots of it), force exactly 1 lot and let
                        # FYERS be the one to accept or reject it -- the
                        # rejection then correctly lands in rejection_log.
                        # If the gate never fired at all (no strike
                        # selected), there is genuinely nothing to trade,
                        # override or not, so this still correctly skips.
                        has_real_strike = sizing.get("selected_strike") is not None and sizing.get("direction") is not None
                        if mode in ("live", "both") and has_real_strike:
                            index_status[index_id] = {"status": "live_override_zero_lots", "reason": sizing.get("note", "0 lots sized by real capital; forcing 1-lot live attempt per explicit override"), "mode": mode}
                            selected_strike = sizing["selected_strike"]
                            direction = sizing["direction"]
                            lots = 1
                            lot_size = sizing["lot_size"]

                            # STRATEGY ENTRY GATES REMOVED (2026-10-07, per explicit
                            # request): the liquidity/spread/volatility gate layer
                            # added 2026-10-01 has been taken out of this decision
                            # path. Confluence + strike selection + position sizing
                            # + the existing risk/circuit-breaker checks are now the
                            # sole authority on whether a fired signal executes.
                            logger.info(f"AUTO-ENTRY APPROVED: index={index_id}, direction={direction}, strike={selected_strike}, lots={lots}")

                            # FIXED A REAL, CONFIRMED BUG (2026-10-01, found by
                            # explicit user report after a live order was
                            # rejected -- IP whitelist -- and NO paper trade
                            # was recorded for it either, even though mode
                            # was "both"). This override path previously
                            # skipped paper entry entirely (old comment here
                            # claimed "0 doesn't make sense for paper", but
                            # that reasoning doesn't hold: by this point
                            # lots has already been forced to 1 for the
                            # live attempt, and that same forced lot count
                            # is exactly what paper tracking should record
                            # too -- paper is supposed to be the permanent
                            # baseline record regardless of what live does,
                            # per this file's own design comments elsewhere
                            # ("Paper entry ALWAYS happens"). A rejected
                            # live order (wrong IP, insufficient margin,
                            # etc.) should never silently erase the paper
                            # record of what the system decided to do.
                            entry_result = execute_paper_entry(
                                index_id, direction, selected_strike, lots, lot_size,
                                quant_confidence=sizing.get("quant_confidence"),
                            )
                            logger.info(f"AUTO-ENTRY (paper, live-override path): {index_id} {direction} {selected_strike['strike']} x{lots} lots — {entry_result}")
                            index_status[index_id]["paper_detail"] = entry_result
                        else:
                            logger.info(f"AUTO-ENTRY RISK VETO: index={index_id} — {sizing.get('note', '0 lots sized')} (existing risk control, not an execution error)")
                            index_status[index_id] = {"status": "no_trade", "reason": sizing.get("note", "0 lots sized"), "mode": mode}
                            continue
                    else:
                        selected_strike = sizing["selected_strike"]
                        direction = sizing["direction"]
                        # FIXED (2026-10-07, per explicit request): sizing["lots"]
                        # reaching this point is already >= 1 -- position sizing's
                        # own affordability check (sizing.get("lots", 0) < 1 above)
                        # is what determines whether 1 lot is genuinely affordable,
                        # and that check has ALREADY passed by the time we're here.
                        # The circuit-breaker size_multiplier is meant to scale DOWN
                        # larger positions during a throttle (e.g. consecutive-loss
                        # multiplier), not to re-introduce a 0-lot outcome for an
                        # already-confirmed-affordable 1-lot trade. int() truncation
                        # previously let int(1 * 0.5) == 0 silently cancel a real,
                        # approved, affordable trade -- max(1, ...) floors the
                        # multiplier's effect at 1 lot instead, while still letting
                        # it reduce 2+ lots normally (e.g. 4 lots * 0.5 -> 2 lots).
                        # The consecutive-loss throttle and circuit breaker
                        # themselves are untouched -- this only changes how their
                        # multiplier interacts with an already-affordable 1 lot.
                        lots = max(1, int(sizing["lots"] * breaker_check["size_multiplier"]))
                        lot_size = sizing["lot_size"]

                        # STRATEGY ENTRY GATES REMOVED (2026-10-07, per explicit
                        # request): the liquidity/spread/volatility gate layer
                        # added 2026-10-01 has been taken out of this decision
                        # path. Confluence + strike selection + position sizing
                        # + the existing risk/circuit-breaker checks are now the
                        # sole authority on whether a fired signal executes.
                        logger.info(f"AUTO-ENTRY APPROVED: index={index_id}, direction={direction}, strike={selected_strike}, lots={lots}")

                        # Paper entry ALWAYS happens (mode paper/live/both all include paper tracking).
                        # quant_confidence (added 2026-09-30, alongside Group G): threaded
                        # down from position_size_signal -> strike_selector_signal ->
                        # confluence_signal, so the confidence score a trade actually
                        # fired at is recorded on the position itself, not discarded
                        # once the fire/no-fire decision was made.
                        entry_result = execute_paper_entry(
                            index_id, direction, selected_strike, lots, lot_size,
                            quant_confidence=sizing.get("quant_confidence"),
                        )
                        logger.info(f"AUTO-ENTRY (paper): {index_id} {direction} {selected_strike['strike']} x{lots} lots — {entry_result}")
                        index_status[index_id] = {"status": "entered_paper", "detail": entry_result, "mode": mode}

                    # Live entry additive, gated on THIS INDEX's own mode --
                    # AND, as of this session, also gated by the account-level
                    # live_trading_master_enabled switch (see
                    # engine/execution/trade_dispatcher.is_live_trading_active_for,
                    # which BOTH this call and the dispatcher itself check --
                    # defense in depth). Routes through trade_dispatcher.execute_entry
                    # rather than the old paper_executor.execute_live_entry, which
                    # was a deliberate placeholder that never placed real orders
                    # (confirmed: it always returned opened=False with a
                    # "not yet implemented" note, regardless of settings).
                    if mode in ("live", "both"):
                        option_symbol = selected_strike.get("symbol", "UNKNOWN")
                        option_type = selected_strike["option_type"]
                        current_premium = selected_strike.get("ltp")

                        if option_symbol == "UNKNOWN" or not current_premium or current_premium <= 0:
                            logger.error(
                                f"AUTO-ENTRY (live attempt) SKIPPED for {index_id}: missing/invalid option_symbol "
                                f"or ltp on selected_strike ({selected_strike}) -- cannot safely place a real "
                                f"order without these. Paper entry above still recorded normally."
                            )
                            index_status[index_id]["live_attempt"] = {
                                "executed": False, "mode": "live",
                                "result": {"status": "error", "message": "missing/invalid option_symbol or ltp on selected_strike"},
                            }
                        else:
                            token = _read_token()
                            live_result = execute_entry(
                                access_token=token, index_id=index_id, fyers_symbol=option_symbol,
                                direction=direction, strike=selected_strike["strike"], option_type=option_type,
                                lots=lots, lot_size=lot_size, simulated_or_live_price=current_premium,
                                quant_confidence=sizing.get("quant_confidence"),
                            )
                            logger.warning(f"AUTO-ENTRY (live attempt): {index_id} — {live_result}")
                            index_status[index_id]["live_attempt"] = live_result

                shared_state.mark_poller_success("decision_loop")

            shared_state.auto_entry_last_cycle = index_status
        except Exception as e:
            shared_state.mark_poller_error("decision_loop", str(e))
            logger.error(f"auto_entry_loop error: {e}")

        await asyncio.sleep(account_config.get("auto_entry_poll_seconds", 10))


def start_auto_entry_loop():
    """Separate startable task — auto-entry is opt-in (disabled by
    default in TRADING_CONFIG), but the loop itself always runs so it
    can pick up config changes without a server restart."""
    asyncio.create_task(auto_entry_loop())
    logger.info("Auto-entry loop started (checks config every cycle, disabled by default)")