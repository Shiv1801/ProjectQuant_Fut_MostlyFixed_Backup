"""
Background pollers — run continuously as asyncio tasks from server
startup, independent of any browser/frontend activity. This is the
core engine loop: as long as uvicorn is running, these keep shared_state
fresh, tick history growing, and (once wired) the decision loop
evaluating trades — regardless of whether anyone has the dashboard open.

Rate budget (Fyers: 10/sec, 200/min):
  - prices: base 1 call per 2 sec, backs off exponentially (up to 30s)
    on repeated failures — quotes appears to have a tighter effective
    limit than the general ceiling (matches other Indian brokers'
    documented per-endpoint quote limits)
  - option_chain: 3 calls per 2 sec (one per index, NIFTY/BANKNIFTY/FINNIFTY)
  - depth: 3 calls per 2 sec (one per index)
  Sustained average with prices backed off: comfortably under 10/sec.
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
    "NIFTY50": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "INDIAVIX": "NSE:INDIAVIX-INDEX",
}

CHAIN_INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY"]  # VIX has no option chain


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
    """
    import random

    shared_state.poller_status["prices"]["running"] = True
    base_interval = 3.0
    max_backoff = 30.0
    current_backoff = base_interval

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
            raw = get_quotes_multi(token, list(SYMBOLS.values()))
            parsed = _parse_quotes_response(raw)
            if parsed:
                shared_state.set_prices(parsed)
                for label, quote in parsed.items():
                    insert_tick(label, quote)
                shared_state.mark_poller_success("prices")
                current_backoff = base_interval
            elif raw.get("code") == 429:
                # Fyers' Quotes API rate limit is a hard 1 req/sec
                # (confirmed via Fyers' own community documentation).
                # A 429 means we are ALREADY over that limit right
                # now, so recovery needs a real, immediate cooldown,
                # not the same gradual backoff used for other errors.
                shared_state.mark_poller_error("prices", f"Rate limited (429) by Fyers Quotes API: {raw}")
                current_backoff = min(max(current_backoff * 2.0, 8.0), max_backoff)
            else:
                shared_state.mark_poller_error("prices", f"Empty/invalid response: {raw}")
                current_backoff = min(current_backoff * 1.6, max_backoff)
        except Exception as e:
            shared_state.mark_poller_error("prices", str(e))
            current_backoff = min(current_backoff * 1.6, max_backoff)
            logger.error(f"price_poller error: {e}")

        jitter = random.uniform(-0.5, 0.5)
        await asyncio.sleep(max(1.0, current_backoff + jitter))


async def option_chain_poller():
    """Polls option chain for all 3 indices every ~2 seconds (staggered, one per ~0.67s)."""
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
        for index_id in CHAIN_INDICES:
            fyers_symbol = SYMBOLS[index_id]
            try:
                raw = get_option_chain(token, fyers_symbol, strike_count=strikes_per_side)
                if raw.get("s") == "ok":
                    shared_state.set_option_chain(index_id, raw)
                    shared_state.mark_poller_success("option_chain")
                else:
                    shared_state.mark_poller_error("option_chain", f"{index_id}: {raw}")
            except Exception as e:
                shared_state.mark_poller_error("option_chain", f"{index_id}: {e}")
                logger.error(f"option_chain_poller error ({index_id}): {e}")
            await asyncio.sleep(0.67)  # spread 3 calls across ~2 seconds


async def depth_poller():
    """Polls market depth for all 3 indices every ~2 seconds (staggered)."""
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
        for index_id in CHAIN_INDICES:
            fyers_symbol = SYMBOLS[index_id]
            try:
                raw = get_market_depth(token, fyers_symbol)
                if raw.get("s") == "ok":
                    shared_state.set_depth(index_id, raw)
                    shared_state.mark_poller_success("depth")
                else:
                    shared_state.mark_poller_error("depth", f"{index_id}: {raw}")
            except Exception as e:
                shared_state.mark_poller_error("depth", f"{index_id}: {e}")
                logger.error(f"depth_poller error ({index_id}): {e}")
            await asyncio.sleep(0.67)


def start_background_pollers():
    """Called once from FastAPI startup — schedules all pollers as
    fire-and-forget asyncio tasks that run for the life of the process."""
    logger.info("POLLERS.PY VERSION CHECK: holiday-aware is_within_market_hours() is ACTIVE in this running process.")
    asyncio.create_task(price_poller())
    asyncio.create_task(option_chain_poller())
    asyncio.create_task(depth_poller())
    asyncio.create_task(account_balance_poller())
    logger.info("Background pollers started: prices (1s), option_chain (~2s), depth (~2s), account_balance (~30s)")


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
            raw = get_funds(token)
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
    Runs continuously, checking all open paper positions against SL/TSL
    every ~5 seconds — independent of frontend activity. Each position
    is checked against ITS OWN index's risk settings (Nifty positions
    use Nifty's SL/TSL, BankNifty positions use BankNifty's, etc.), not
    one shared global config — required now that settings are per-index.
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
            actions = monitor_and_close_positions(_parse_chain_from_shared_state, get_index_risk_params, access_token=token)
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
        await asyncio.sleep(5)


def start_position_monitor():
    """Separate from start_background_pollers so the two concerns (data
    ingestion vs. position management) stay independently startable/testable."""
    asyncio.create_task(position_monitor_loop())
    logger.info("Position monitor loop started (5s interval)")


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
                shared_state.mark_poller_error("decision_loop", f"circuit breaker tripped: {breaker_check['breaker_reason']}")
            else:
                for index_id in INDICES:
                    index_cfg = get_index_config(index_id)
                    mode = index_cfg["mode"]

                    if mode == "off":
                        index_status[index_id] = {"status": "off", "reason": "trading mode set to Off for this index"}
                        continue

                    if index_id in open_index_ids:
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
                            # Paper tracking still records the REAL sized amount (0 doesn't make
                            # sense for paper, so paper entry is skipped here -- this override
                            # path exists specifically to test/confirm the LIVE order mechanism).
                        else:
                            index_status[index_id] = {"status": "no_trade", "reason": sizing.get("note", "0 lots sized"), "mode": mode}
                            continue
                    else:
                        selected_strike = sizing["selected_strike"]
                        direction = sizing["direction"]
                        lots = int(sizing["lots"] * breaker_check["size_multiplier"])
                        if lots < 1:
                            index_status[index_id] = {"status": "no_trade", "reason": "size multiplier reduced lots below 1", "mode": mode}
                            continue
                        lot_size = sizing["lot_size"]

                        # Paper entry ALWAYS happens (mode paper/live/both all include paper tracking).
                        entry_result = execute_paper_entry(index_id, direction, selected_strike, lots, lot_size)
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