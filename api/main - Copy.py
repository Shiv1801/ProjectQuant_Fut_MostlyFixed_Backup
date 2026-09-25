"""
Step 28: Full rewrite — background pollers now run continuously from
server startup (independent of frontend activity), writing into
shared_state. Every /signals/* and /market/* endpoint now reads from
shared_state instead of calling Fyers directly. This eliminates the
rate-limit risk entirely (Fyers is only ever called by the 3 pollers,
never by request handlers) and means data keeps flowing even with no
browser open, as long as uvicorn is running.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
from loguru import logger
from datetime import datetime, timedelta
from pathlib import Path
import math

from config.db import (
    init_db, insert_ping, get_recent_pings,
    insert_tick, get_recent_ticks, get_tick_count,
    get_recent_candles, get_candle_count, insert_historical_candles,
    insert_atm_iv_snapshot, get_iv_history, get_iv_history_count,
    get_open_positions, get_trade_log, get_trade_log_stats, get_trade_log_detailed_stats, clear_trade_history,
    insert_backtest_run, get_backtest_runs, mark_backtest_run_applied,
    get_trade_action_log, get_rejection_log, get_position,
)
from engine.execution.trade_dispatcher import execute_exit
from engine.data_layer.fyers_client.auth import get_fyers_model, generate_auth_url, exchange_auth_code_for_token
from engine.data_layer.fyers_client.rest_client import get_historical_candles
from engine.data_layer.historical_backfill import backfill_index, backfill_index_extended
from engine.shared_state.store import shared_state
from engine.background.pollers import start_background_pollers, start_position_monitor, start_auto_entry_loop, SYMBOLS, CHAIN_INDICES

from engine.signal_layer.volatility.realized_vol import compute_realized_vol
from engine.signal_layer.volatility.garch import compute_garch_forecast
from engine.signal_layer.vrp_pricing.vrp import compute_vrp
from engine.signal_layer.vrp_pricing.iv_rank import compute_iv_rank
from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.signal_layer.momentum.variance_ratio import compute_variance_ratio
from engine.signal_layer.momentum.ofi import compute_ofi
from engine.signal_layer.market_structure.gex import compute_gex
from engine.signal_layer.market_structure.pcr import compute_pcr
from engine.signal_layer.market_structure.max_pain import compute_max_pain
from engine.signal_layer.market_structure.oi_change import compute_oi_change_analysis
from engine.signal_layer.regime.vix_regime import compute_vix_regime
from engine.signal_layer.regime.rv_iv_ratio import compute_rv_iv_ratio
from engine.signal_layer.regime.correlation import compute_correlation_breakdown
from engine.signal_layer.position_sizing.kelly import compute_fractional_kelly
from engine.signal_layer.position_sizing.vol_scaled import compute_vol_scaled_size
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure
from engine.confluence_gate.gate import evaluate_confluence
from engine.strike_selector.selector import select_best_strike
from engine.risk_engine.position_sizing import compute_position_size
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop
from engine.risk_engine.circuit_breakers import evaluate_circuit_breakers, check_weekly_drawdown
from engine.execution.paper_executor import execute_paper_entry
from config.trading_config import (
    get_index_config, get_all_index_configs, get_index_capital,
    update_index_risk_params, update_index_mode, update_capital_allocations,
    reset_index_config, reset_all_index_configs, get_account_config, update_account_config, INDICES,
)
from engine.backtest.engine import run_backtest

app = FastAPI(title="Project Trade API", version="0.3.0")
_SERVER_START_TIME = datetime.utcnow().isoformat()  # lets /health prove a fresh restart actually happened


@app.websocket("/ws/prices")
async def prices_websocket(websocket: WebSocket):
    """
    Real-time price push to the browser. Built to make the backend's
    WebSocket-driven price_poller (now writing ticks in real time,
    replacing the old REST-polling approach) actually reach the screen
    in real time too -- previously the frontend only polled every 2
    seconds via setInterval(refreshAll), so even sub-second backend
    data was invisible until the next poll tick. This pushes a price
    snapshot the moment shared_state.prices changes, checked every
    250ms (not truly zero-latency, but far tighter than a multi-second
    poll, and avoids re-sending identical data when nothing changed).
    """
    await websocket.accept()
    last_sent = None
    try:
        while True:
            current = shared_state.prices
            if current != last_sent:
                await websocket.send_json({"type": "prices", "data": current})
                last_sent = dict(current)
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"prices_websocket error: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOKEN_PATH = Path(".fyers_token")


def _read_token() -> str | None:
    if not TOKEN_PATH.exists():
        return None
    return TOKEN_PATH.read_text().strip()


@app.on_event("startup")
def on_startup():
    init_db()
    start_background_pollers()
    start_position_monitor()
    start_auto_entry_loop()


@app.get("/health")
async def health():
    # Reports whether key newer capabilities exist on THIS running server,
    # so you can immediately tell if you're looking at a stale process
    # instead of guessing from a hardcoded step number that stops being
    # updated (this was a real source of confusion — a previous hardcoded
    # "step: 28" label never changed across many later updates, so it
    # looked like the server was outdated even when the actual code was
    # current — the label was just stale, not the code).
    return {
        "status": "ok",
        "server_started_at": _SERVER_START_TIME,
        "capabilities": {
            "per_index_config": "get_index_config" in globals(),
            "clear_trade_history": "clear_trade_history" in globals(),
        },
    }


@app.get("/db-check")
async def db_check():
    insert_ping(f"ping at {datetime.utcnow().isoformat()}")
    recent = get_recent_pings(limit=5)
    return {"status": "ok", "recent_pings": recent}


@app.get("/pollers/status")
async def pollers_status():
    """Lets the frontend show whether background pollers are alive, how
    fresh each data type is, and whether Fyers has explicitly rejected
    the current token (distinct from rate-limit or network errors)."""
    return {
        "status": "ok",
        "pollers": shared_state.poller_status,
        "prices_updated_at": shared_state.prices_updated_at,
        "option_chain_ages_seconds": {
            idx: shared_state.get_option_chain_age_seconds(idx) for idx in CHAIN_INDICES
        },
        "auth_error_detected": shared_state.has_auth_error(),
    }


# ---------------- Auth / token management (unchanged from step 15) ----------------

@app.get("/auth/status")
async def auth_status():
    if not TOKEN_PATH.exists():
        return {"status": "ok", "has_token": False, "token_age_hours": None}

    IST_OFFSET_HOURS = 5.5
    now_ist = datetime.utcnow() + timedelta(hours=IST_OFFSET_HOURS)
    most_recent_8am_ist = now_ist.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_ist < most_recent_8am_ist:
        most_recent_8am_ist -= timedelta(days=1)

    mtime_utc = datetime.utcfromtimestamp(TOKEN_PATH.stat().st_mtime)
    mtime_ist = mtime_utc + timedelta(hours=IST_OFFSET_HOURS)

    age_hours = round((now_ist - mtime_ist).total_seconds() / 3600, 1)
    is_stale = mtime_ist < most_recent_8am_ist

    return {"status": "ok", "has_token": True, "token_age_hours": age_hours, "is_stale": is_stale}


@app.get("/auth/login-url")
async def auth_login_url():
    try:
        url = generate_auth_url()
        return {"status": "ok", "login_url": url}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class AuthCodeRequest(BaseModel):
    auth_code: str


@app.post("/auth/exchange")
async def auth_exchange(req: AuthCodeRequest):
    raw = req.auth_code.strip()
    if "auth_code=" in raw:
        code = raw.split("auth_code=")[1].split("&")[0]
    else:
        code = raw

    try:
        token = exchange_auth_code_for_token(code)
        TOKEN_PATH.write_text(token)
        return {"status": "ok", "message": "Token saved successfully."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/fyers/profile")
async def fyers_profile():
    token = _read_token()
    if not token:
        return {"status": "error", "message": "No token found. Log in via the app first."}
    fyers = get_fyers_model(token)
    return {"status": "ok", "profile": fyers.get_profile()}


# ---------------- Market data — now reads shared_state, no Fyers calls here ----------------

@app.get("/market/all")
async def market_all():
    """Reads shared_state (populated by the price_poller running every 1s in the background)."""
    if not shared_state.prices:
        return {"status": "error", "message": "No price data yet — waiting for background poller (needs a valid token)."}
    return {"status": "ok", "data": shared_state.prices, "fetched_at": shared_state.prices_updated_at}


@app.get("/ticks/{index_id}")
async def ticks_for_index(index_id: str, limit: int = 20):
    rows = get_recent_ticks(index_id.upper(), limit=limit)
    return {"status": "ok", "index_id": index_id.upper(), "count": len(rows), "ticks": rows}


@app.get("/ticks/count/all")
async def ticks_count_all():
    counts = {label: get_tick_count(label) for label in SYMBOLS}
    return {"status": "ok", "counts": counts, "total": get_tick_count()}


def _parse_chain_from_shared_state(index_id: str) -> dict | None:
    """Same parsing logic as before, now applied to the cached shared_state
    chain response instead of a fresh Fyers call."""
    raw = shared_state.get_option_chain(index_id)
    if not raw or raw.get("s") != "ok":
        return None

    data = raw.get("data", {})
    chain = data.get("optionsChain", [])

    spot = None
    for row in chain:
        if row.get("strike_price") == -1:
            spot = row.get("ltp")
            break

    strikes: dict[float, dict] = {}
    for row in chain:
        strike = row.get("strike_price")
        if strike is None or strike <= 0:
            continue
        strikes.setdefault(strike, {"strike": strike, "CE": None, "PE": None})
        side = "CE" if row.get("option_type") == "CE" else "PE"
        greeks = row.get("greeks") or {}
        strikes[strike][side] = {
            "symbol": row.get("symbol"),
            "ltp": row.get("ltp"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "oi": row.get("oi"),
            "oi_change": row.get("oich"),
            "volume": row.get("volume"),
            "iv": greeks.get("iv"),
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "theta": greeks.get("theta"),
            "vega": greeks.get("vega"),
        }

    return {
        "spot": spot,
        "call_oi_total": data.get("callOi"),
        "put_oi_total": data.get("putOi"),
        "expiry_data": data.get("expiryData", []),
        "strikes": sorted(strikes.values(), key=lambda r: r["strike"]),
    }


@app.get("/market/option-chain")
async def option_chain(index: str = "NIFTY50"):
    index = index.upper()
    if index not in CHAIN_INDICES:
        return {"status": "error", "message": f"'{index}' is not a valid option-chain index."}

    parsed = _parse_chain_from_shared_state(index)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet — waiting for background poller."}

    age = shared_state.get_option_chain_age_seconds(index)
    return {
        "status": "ok", "index": index,
        "call_oi_total": parsed["call_oi_total"], "put_oi_total": parsed["put_oi_total"],
        "strikes": parsed["strikes"],
        "fetched_at": shared_state.option_chains_updated_at.get(index),
        "age_seconds": round(age, 2) if age is not None else None,
    }


@app.get("/candles/{index_id}")
async def candles_for_index(index_id: str, limit: int = 60):
    rows = get_recent_candles(index_id.upper(), limit=limit)
    return {"status": "ok", "index_id": index_id.upper(), "count": len(rows), "candles": rows}


@app.get("/backfill/all")
async def backfill_all(days: int = 30):
    token = _read_token()
    if not token:
        return {"status": "error", "message": "No token found. Log in via the app first."}
    if days > 90:
        return {"status": "error", "message": "Fyers limits 1-min history to ~100 days per call. Use days<=90."}

    results = {}
    for index_id, fyers_symbol in SYMBOLS.items():
        if index_id == "INDIAVIX":
            continue
        results[index_id] = backfill_index(
            rest_client_fn=get_historical_candles, access_token=token, fyers_symbol=fyers_symbol,
            index_id=index_id, days_back=days, db_insert_fn=insert_historical_candles,
        )
    return {"status": "ok", "results": results, "completed_at": datetime.utcnow().isoformat()}


@app.get("/candles/count/all")
async def candle_count_all():
    counts = {label: get_candle_count(label) for label in SYMBOLS if label != "INDIAVIX"}
    return {"status": "ok", "counts": counts}


@app.get("/backfill/{index_id}")
async def backfill(index_id: str, days: int = 30):
    token = _read_token()
    if not token:
        return {"status": "error", "message": "No token found. Log in via the app first."}
    index_id = index_id.upper()
    fyers_symbol = SYMBOLS.get(index_id)
    if not fyers_symbol or index_id == "INDIAVIX":
        return {"status": "error", "message": f"'{index_id}' is not a valid backfill target."}
    if days > 90:
        return {"status": "error", "message": "Use days<=90."}

    result = backfill_index(
        rest_client_fn=get_historical_candles, access_token=token, fyers_symbol=fyers_symbol,
        index_id=index_id, days_back=days, db_insert_fn=insert_historical_candles,
    )
    return result


@app.get("/backfill/extended/{index_id}")
async def backfill_extended(index_id: str, days: int = 180):
    """
    Extended, multi-chunk backfill for spans beyond Fyers' ~90-100 day
    per-request limit -- built to pull a real, multi-month volume
    history for NIFTY50 so VWAP has genuine same-day-onward data from
    the first trading day the app runs after this completes, rather
    than needing to accumulate volume live from a cold start. Chains
    multiple chunked requests; a single chunk failing does not abort
    the whole backfill (each chunk's success/failure is tracked and
    reported).
    """
    token = _read_token()
    if not token:
        return {"status": "error", "message": "No token found. Log in via the app first."}
    index_id = index_id.upper()
    fyers_symbol = SYMBOLS.get(index_id)
    if not fyers_symbol or index_id == "INDIAVIX":
        return {"status": "error", "message": f"'{index_id}' is not a valid backfill target."}
    if days < 1 or days > 1825:
        return {"status": "error", "message": "days must be between 1 and 1825 (5 years)."}

    result = backfill_index_extended(
        rest_client_fn=get_historical_candles, access_token=token, fyers_symbol=fyers_symbol,
        index_id=index_id, days_back=days, db_insert_fn=insert_historical_candles,
    )
    return result


@app.get("/candles/count/{index_id}")
async def candle_count(index_id: str):
    return {"status": "ok", "index_id": index_id.upper(), "count": get_candle_count(index_id.upper())}


# ---------------- Group A ----------------

@app.get("/signals/realized-vol/{index_id}")
async def realized_vol_signal(index_id: str, window: int = 30):
    candles = get_recent_candles(index_id.upper(), limit=window)
    closes = [c["close"] for c in candles if c["close"] is not None]
    result = compute_realized_vol(closes)
    return {"status": "ok", "index_id": index_id.upper(), "model_id": "realized_vol_multiwindow", "group": "A",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/garch-vol/{index_id}")
async def garch_vol_signal(index_id: str, window: int = 100):
    candles = get_recent_candles(index_id.upper(), limit=window)
    closes = [c["close"] for c in candles if c["close"] is not None]
    result = compute_garch_forecast(closes)
    return {"status": "ok", "index_id": index_id.upper(), "model_id": "garch_1_1", "group": "A",
            **result, "computed_at": datetime.utcnow().isoformat()}


# ---------------- Group B — now reads shared_state chain, zero Fyers calls ----------------

def _parse_expiry_epoch(expiry_raw):
    if expiry_raw is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(expiry_raw))
    except (ValueError, TypeError, OSError):
        return None


@app.get("/signals/vrp/{index_id}")
async def vrp_signal(index_id: str):
    index_id = index_id.upper()
    if index_id not in CHAIN_INDICES:
        return {"status": "error", "message": f"'{index_id}' is not a valid VRP target."}

    parsed = _parse_chain_from_shared_state(index_id)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet — waiting for background poller."}

    spot = parsed["spot"]
    strikes = parsed["strikes"]
    if spot is None or not strikes:
        return {"status": "error", "message": "Incomplete chain data (missing spot or strikes)."}

    atm_strike = min((s["strike"] for s in strikes), key=lambda k: abs(k - spot))
    atm_row = next((s for s in strikes if s["strike"] == atm_strike), None)
    atm_ce = (atm_row or {}).get("CE") or {}
    atm_iv_pct = atm_ce.get("iv")

    if atm_iv_pct is None:
        return {"status": "error", "message": "No IV available for ATM strike."}

    insert_atm_iv_snapshot(index_id, atm_strike, atm_iv_pct)

    candles = get_recent_candles(index_id, limit=100)
    closes = [c["close"] for c in candles if c["close"] is not None]
    garch_result = compute_garch_forecast(closes)
    garch_forecast_pct = garch_result.get("garch_forecast_vol_pct")

    vrp_result = compute_vrp(atm_iv_pct, garch_forecast_pct)

    expiry_dts = [d for d in (_parse_expiry_epoch(e.get("expiry")) for e in parsed["expiry_data"]) if d is not None]
    days_to_expiry = round((min(expiry_dts) - datetime.utcnow()).total_seconds() / 86400, 1) if expiry_dts else None

    return {
        "status": "ok", "index_id": index_id, "model_id": "variance_risk_premium", "group": "B",
        "spot": spot, "atm_strike": atm_strike, "atm_ce_ltp": atm_ce.get("ltp"),
        "atm_greeks": atm_ce, "time_to_expiry_days": days_to_expiry,
        **vrp_result, "computed_at": datetime.utcnow().isoformat(),
    }


@app.get("/signals/iv-rank/{index_id}")
async def iv_rank_signal(index_id: str):
    index_id = index_id.upper()
    history_rows = get_iv_history(index_id, limit=500)
    historical_ivs = [r["atm_iv_pct"] for r in history_rows if r["atm_iv_pct"] is not None]
    current_iv = historical_ivs[-1] if historical_ivs else None
    result = compute_iv_rank(current_iv, historical_ivs[:-1] if historical_ivs else [])
    return {"status": "ok", "index_id": index_id, "model_id": "iv_rank_percentile", "group": "B",
            "current_iv_pct": current_iv, **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/iv-history/count/{index_id}")
async def iv_history_count(index_id: str):
    return {"status": "ok", "index_id": index_id.upper(), "count": get_iv_history_count(index_id.upper())}


# ---------------- Group C ----------------

@app.get("/signals/hurst/{index_id}")
async def hurst_signal(index_id: str, window: int = 100):
    candles = get_recent_candles(index_id.upper(), limit=window)
    closes = [c["close"] for c in candles if c["close"] is not None]
    result = compute_hurst_exponent(closes)
    return {"status": "ok", "index_id": index_id.upper(), "model_id": "hurst_exponent", "group": "C",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/zscore/{index_id}")
async def zscore_signal(index_id: str, window: int = 20):
    candles = get_recent_candles(index_id.upper(), limit=window)
    closes = [c["close"] for c in candles if c["close"] is not None]
    result = compute_zscore(closes, window=window)
    return {"status": "ok", "index_id": index_id.upper(), "model_id": "zscore_price_deviation", "group": "C",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/variance-ratio/{index_id}")
async def variance_ratio_signal(index_id: str, window: int = 150, q: int = 5):
    candles = get_recent_candles(index_id.upper(), limit=window)
    closes = [c["close"] for c in candles if c["close"] is not None]
    result = compute_variance_ratio(closes, q=q)
    return {"status": "ok", "index_id": index_id.upper(), "model_id": "autocorrelation_variance_ratio", "group": "C",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/ofi/{index_id}")
async def ofi_signal(index_id: str):
    index_id = index_id.upper()
    depth_data = shared_state.get_depth(index_id)
    if not depth_data:
        return {"status": "error", "message": "No depth data yet — waiting for background poller."}

    fyers_symbol = SYMBOLS.get(index_id)
    inner = (depth_data.get("d") or {}).get(fyers_symbol, {})
    total_buy = inner.get("totalbuyqty")
    total_sell = inner.get("totalsellqty")

    result = compute_ofi(total_buy, total_sell)
    return {"status": "ok", "index_id": index_id, "model_id": "order_flow_imbalance", "group": "C",
            **result, "computed_at": datetime.utcnow().isoformat()}


# ---------------- Group D — all read shared_state chain ----------------

@app.get("/signals/gex/{index_id}")
async def gex_signal(index_id: str):
    index_id = index_id.upper()
    parsed = _parse_chain_from_shared_state(index_id)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet."}
    result = compute_gex(parsed["strikes"], parsed["spot"])
    return {"status": "ok", "index_id": index_id, "model_id": "gamma_exposure", "group": "D",
            "spot": parsed["spot"], **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/pcr/{index_id}")
async def pcr_signal(index_id: str):
    index_id = index_id.upper()
    parsed = _parse_chain_from_shared_state(index_id)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet."}
    result = compute_pcr(parsed["call_oi_total"], parsed["put_oi_total"])
    return {"status": "ok", "index_id": index_id, "model_id": "put_call_ratio_oi", "group": "D",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/max-pain/{index_id}")
async def max_pain_signal(index_id: str):
    index_id = index_id.upper()
    parsed = _parse_chain_from_shared_state(index_id)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet."}
    result = compute_max_pain(parsed["strikes"])
    return {"status": "ok", "index_id": index_id, "model_id": "max_pain", "group": "D",
            "spot": parsed["spot"], **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/oi-change/{index_id}")
async def oi_change_signal(index_id: str):
    index_id = index_id.upper()
    parsed = _parse_chain_from_shared_state(index_id)
    if parsed is None:
        return {"status": "error", "message": "No option chain data yet."}
    result = compute_oi_change_analysis(parsed["strikes"], top_n=5)
    return {"status": "ok", "index_id": index_id, "model_id": "oi_change_analysis", "group": "D",
            **result, "computed_at": datetime.utcnow().isoformat()}


# ---------------- Group E ----------------

@app.get("/signals/vix-regime")
async def vix_regime_signal(window: int = 20):
    candles = get_recent_candles("INDIAVIX", limit=window)
    result = compute_vix_regime(candles)
    return {"status": "ok", "model_id": "vix_level_roc", "group": "E",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/rv-iv-ratio/{index_id}")
async def rv_iv_ratio_signal(index_id: str):
    index_id = index_id.upper()
    candles = get_recent_candles(index_id, limit=30)
    closes = [c["close"] for c in candles if c["close"] is not None]
    rv_result = compute_realized_vol(closes)
    realized_vol_pct = rv_result.get("realized_vol_pct")

    iv_history = get_iv_history(index_id, limit=1)
    implied_vol_pct = iv_history[-1]["atm_iv_pct"] if iv_history else None

    result = compute_rv_iv_ratio(realized_vol_pct, implied_vol_pct)
    return {"status": "ok", "index_id": index_id, "model_id": "realized_implied_ratio", "group": "E",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/correlation-breakdown")
async def correlation_breakdown_signal(window: int = 20):
    """
    DISABLED per explicit request: this app now trades NIFTY50 only
    (BankNifty and FinNifty removed). Correlation Breakdown inherently
    needs multiple indices to compare against each other — with only
    one index left, there is nothing to correlate. Returns an honest
    "not applicable" response rather than silently fetching data for
    indices that no longer exist in this app.
    """
    return {
        "status": "not_applicable", "model_id": "correlation_breakdown", "group": "E",
        "message": "Correlation Breakdown is disabled — this app trades NIFTY50 only, "
                    "and this model requires multiple indices to compare.",
        "computed_at": datetime.utcnow().isoformat(),
    }


# ---------------- Group F ----------------

@app.get("/signals/kelly-sizing")
async def kelly_sizing_signal():
    """Group F: Fractional Kelly — now reads REAL trade_log stats (Step 36+)."""
    stats = get_trade_log_stats()
    result = compute_fractional_kelly(
        stats.get("win_rate"), stats.get("avg_win_pct"), stats.get("avg_loss_pct"),
        trade_count=stats.get("trade_count", 0),
    )
    return {"status": "ok", "model_id": "fractional_kelly", "group": "F",
            **result, "computed_at": datetime.utcnow().isoformat()}


@app.get("/signals/vol-scaled-sizing/{index_id}")
async def vol_scaled_sizing_signal(index_id: str, base_size: float = 100000, window: int = 30):
    index_id = index_id.upper()
    candles = get_recent_candles(index_id, limit=200)
    closes = [c["close"] for c in candles if c["close"] is not None]

    current_result = compute_realized_vol(closes[-window:] if len(closes) >= window else closes)
    current_vol = current_result.get("realized_vol_pct")

    reference_result = compute_realized_vol(closes)
    reference_vol = reference_result.get("realized_vol_pct")

    result = compute_vol_scaled_size(base_size, current_vol, reference_vol)
    return {"status": "ok", "index_id": index_id, "model_id": "vol_scaled_sizing", "group": "F",
            **result, "computed_at": datetime.utcnow().isoformat()}


# ---------------- Confluence Gate ----------------

@app.get("/signals/confluence/{index_id}")
async def confluence_signal(index_id: str, min_confidence: float = 0.5):
    """
    PRD §4.3.5 — the dual-leg AND gate, now with an optional Leg 3
    (VWAP), per-index (defaults ON for NIFTY50 per explicit request —
    see config/trading_config.py). Gathers already-computed model
    outputs (no new Fyers calls, everything here is either shared_state
    or fast DB reads), scores Leg 1 (quant confidence), checks Leg 2
    (chart structure), checks Leg 3 (VWAP) if enabled for this index,
    and returns the combined fire/no-fire decision with full
    transparency into all legs.
    """
    index_id = index_id.upper()

    candles = get_recent_candles(index_id, limit=100)
    closes = [c["close"] for c in candles if c["close"] is not None]

    hurst_result = compute_hurst_exponent(closes)
    zscore_result = compute_zscore(closes, window=20)

    depth_data = shared_state.get_depth(index_id)
    ofi_result = {"ofi": None}
    if depth_data:
        fyers_symbol = SYMBOLS.get(index_id)
        inner = (depth_data.get("d") or {}).get(fyers_symbol, {})
        ofi_result = compute_ofi(inner.get("totalbuyqty"), inner.get("totalsellqty"))

    parsed_chain = _parse_chain_from_shared_state(index_id) if index_id in CHAIN_INDICES else None
    pcr_result = {"pcr": None}
    if parsed_chain:
        pcr_result = compute_pcr(parsed_chain["call_oi_total"], parsed_chain["put_oi_total"])

    model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": ofi_result, "pcr": pcr_result}
    quant_result = compute_quant_confidence(model_outputs)

    chart_result = {"verdict": "no_quant_direction", "confirmed": False}
    if quant_result.get("direction"):
        chart_result = confirm_chart_structure(candles, quant_result["direction"])

    index_config = get_index_config(index_id)
    require_vwap = index_config["risk_params"].get("require_vwap", False) if index_config else False

    vwap_result = None
    if require_vwap:
        vwap_result = _compute_intraday_vwap(index_id, candles)

    gate_result = evaluate_confluence(
        quant_result, chart_result, min_confidence_threshold=min_confidence,
        vwap_result=vwap_result, require_vwap=require_vwap,
    )

    return {
        "status": "ok", "index_id": index_id, "model_id": "confluence_gate",
        **gate_result, "computed_at": datetime.utcnow().isoformat(),
    }


def _compute_intraday_vwap(index_id: str, candles: list[dict]) -> dict | None:
    """
    Computes today's real, live intraday VWAP from the app's own candle
    history — resets at the start of each trading day, using genuine
    cumulative daily volume (captured from Fyers' live quotes, see
    engine/background/pollers.py's insert_tick volume capture). Returns
    None if there isn't yet enough same-day volume data (e.g. the very
    first minute or two of trading) — treated as "no signal yet" by
    the gate, never a veto.
    """
    if not candles:
        return None
    today = _now_ist().date()
    todays_candles = [c for c in candles if _minute_bucket_to_date(c["minute_bucket"]) == today]
    if len(todays_candles) < 2:
        return None

    cumulative_pv = 0.0
    cumulative_vol = 0.0
    prev_volume = None
    for c in todays_candles:
        vol = c.get("volume")
        if vol is None:
            continue
        minute_volume = vol - prev_volume if prev_volume is not None else 0
        prev_volume = vol
        if minute_volume < 0:
            continue
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        cumulative_pv += typical_price * minute_volume
        cumulative_vol += minute_volume

    if cumulative_vol <= 0:
        return None
    vwap = cumulative_pv / cumulative_vol
    current_price = todays_candles[-1]["close"]
    return {"vwap": round(vwap, 2), "price": current_price}


def _minute_bucket_to_date(minute_bucket: str):
    return datetime.strptime(minute_bucket[:10], "%Y-%m-%d").date()


def _now_ist():
    IST_OFFSET_HOURS = 5.5
    return datetime.utcnow() + timedelta(hours=IST_OFFSET_HOURS)


# ---------------- Strike Selector ----------------

@app.get("/signals/strike-selector/{index_id}")
async def strike_selector_signal(index_id: str, direction: str = None, min_confidence: float = 0.5):
    """
    PRD §4.4 — picks the best strike to trade for a given direction.
    If `direction` isn't explicitly passed, runs the confluence gate
    first and uses its direction (only if it actually fired).
    """
    index_id = index_id.upper()
    if index_id not in CHAIN_INDICES:
        return {"status": "error", "message": f"'{index_id}' has no option chain to select from."}

    if not direction:
        gate_response = await confluence_signal(index_id, min_confidence=min_confidence)
        if not gate_response.get("fired"):
            return {
                "status": "ok", "index_id": index_id, "model_id": "strike_selector",
                "selected": None, "note": f"confluence gate has not fired (veto_reason={gate_response.get('veto_reason')}) — no direction to select a strike for",
                "computed_at": datetime.utcnow().isoformat(),
            }
        direction = gate_response["direction"]

    if direction not in ("long_call", "long_put"):
        return {"status": "error", "message": f"Invalid direction: {direction}"}

    parsed_chain = _parse_chain_from_shared_state(index_id)
    if parsed_chain is None:
        return {"status": "error", "message": "No option chain data yet — waiting for background poller."}

    iv_history = get_iv_history(index_id, limit=500)
    historical_ivs = [r["atm_iv_pct"] for r in iv_history if r["atm_iv_pct"] is not None]
    current_iv = historical_ivs[-1] if historical_ivs else None
    iv_rank_result = compute_iv_rank(current_iv, historical_ivs[:-1] if historical_ivs else [])
    iv_rank = iv_rank_result.get("iv_rank")

    result = select_best_strike(parsed_chain["strikes"], direction, iv_rank=iv_rank)

    return {
        "status": "ok", "index_id": index_id, "model_id": "strike_selector",
        "direction": direction, "spot": parsed_chain["spot"],
        **result, "computed_at": datetime.utcnow().isoformat(),
    }


# ---------------- Risk Engine ----------------

@app.get("/signals/position-size/{index_id}")
async def position_size_signal(index_id: str, available_capital: float = 100000,
                                risk_per_trade_pct: float = 2.0, hard_sl_pct: float = 27.0,
                                min_confidence: float = 0.5):
    """
    PRD §4.5, §4.8.6 — automatically computes how many lots to trade,
    chaining confluence gate -> strike selector -> position sizing.
    If the gate hasn't fired, returns 0 lots with a clear reason rather
    than a fabricated size.
    """
    index_id = index_id.upper()

    strike_response = await strike_selector_signal(index_id, direction=None, min_confidence=min_confidence)
    if not strike_response.get("selected"):
        return {
            "status": "ok", "index_id": index_id, "model_id": "position_sizing",
            "lots": 0, "note": strike_response.get("note") or "no strike selected (gate likely hasn't fired)",
            "computed_at": datetime.utcnow().isoformat(),
        }

    selected = strike_response["selected"]
    premium = selected["ltp"]

    kelly_response = await kelly_sizing_signal()
    kelly_scale = None
    if kelly_response.get("sized_fraction") is not None:
        # Kelly's sized_fraction is itself already a small %, use it as a
        # direct multiplier on the base risk-per-trade (neutral fallback
        # to 1.0 handled inside compute_position_size when None).
        kelly_scale = kelly_response["sized_fraction"] * 4  # normalize so ~0.25 sizing -> ~1.0x multiplier

    vol_response = await vol_scaled_sizing_signal(index_id, base_size=100000, window=30)
    vol_scale = vol_response.get("scale")

    sizing_result = compute_position_size(
        available_capital=available_capital,
        index_id=index_id,
        premium_per_share=premium,
        hard_sl_pct=hard_sl_pct,
        risk_per_trade_pct=risk_per_trade_pct,
        kelly_scale=kelly_scale,
        vol_scale=vol_scale,
    )

    return {
        "status": "ok", "index_id": index_id, "model_id": "position_sizing",
        "direction": strike_response.get("direction"), "selected_strike": selected,
        **sizing_result, "computed_at": datetime.utcnow().isoformat(),
    }


@app.get("/signals/circuit-breakers")
async def circuit_breakers_signal(daily_pnl_pct: float = 0.0, consecutive_losses: int = 0,
                                   open_position_count: int = 0):
    """
    PRD §4.8.4 — account-level circuit breakers. Until the execution
    layer exists (no trades happening yet), these inputs default to
    "clean slate" values — the endpoint itself is fully functional and
    ready to receive real numbers once paper trading is wired up.
    """
    result = evaluate_circuit_breakers(
        daily_pnl_pct=daily_pnl_pct, consecutive_losses=consecutive_losses,
        open_position_count=open_position_count,
    )
    return {"status": "ok", "model_id": "circuit_breakers", **result, "computed_at": datetime.utcnow().isoformat()}


# ---------------- Execution (Paper Trading) ----------------

@app.get("/positions/open")
async def positions_open(index_id: str = None):
    rows = get_open_positions(index_id.upper() if index_id else None)
    return {"status": "ok", "count": len(rows), "positions": rows}


@app.get("/trade-log")
async def trade_log(limit: int = 100):
    rows = get_trade_log(limit=limit)
    return {"status": "ok", "count": len(rows), "trades": rows}


@app.get("/trade-log/{position_id}/actions")
async def trade_actions(position_id: int):
    """
    Full, timestamped action history for one specific trade — powers
    the "expand a trade to see detailed timestamps" view in the Trade
    Log, for both open and closed trades, per explicit request.
    Works identically for open positions (shows entry actions and any
    TSL updates so far, no exit yet) and closed ones (additionally
    shows exit_triggered/exit_filled as the last entries).
    """
    actions = get_trade_action_log(position_id)
    return {"status": "ok", "position_id": position_id, "actions": actions}


@app.get("/rejection-log")
async def rejection_log(limit: int = 100):
    """
    Real order rejections — separate from trade_log, since a rejected
    order was never a real trade (no position ever opened). Entries
    only exist for genuine, real (live-mode) order attempts that
    failed; always empty for a purely paper-trading account.
    """
    rejections = get_rejection_log(limit=limit)
    return {"status": "ok", "count": len(rejections), "rejections": rejections}


class ManualExitRequest(BaseModel):
    current_price: float


@app.post("/positions/{position_id}/exit")
async def manual_exit_position(position_id: int, request: ManualExitRequest):
    """
    Manual "Exit Trade" button for open positions. Routes through the
    SAME trade_dispatcher.execute_exit used by every automatic exit —
    a manual exit on a REAL (mode="live") position places a REAL sell
    order on Fyers, identically safety-checked and logged as any other
    real exit. A manual exit on a paper position simply closes the
    simulated position, no real order involved.

    current_price is supplied by the caller (the frontend already has
    the live LTP on screen for the position being exited) rather than
    re-fetched here, avoiding a redundant Fyers call.
    """
    position = get_position(position_id)
    if position is None:
        return {"status": "error", "message": f"No position found with id={position_id}"}
    if position["status"] != "open":
        return {"status": "error", "message": f"Position {position_id} is already {position['status']}, cannot exit again."}

    entry_price = position["entry_price"]
    lots = position["lots"]
    lot_size = position["lot_size"]
    current_price = request.current_price

    pnl = (current_price - entry_price) * lots * lot_size
    pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

    token = _read_token()
    result = execute_exit(
        access_token=token, index_id=position["index_id"], position_id=position_id,
        fyers_symbol=position["option_symbol"], lots=lots, lot_size=lot_size,
        exit_price=current_price, exit_reason="manual_exit", pnl=pnl, pnl_pct=pnl_pct,
        position_mode=position["mode"],
    )

    if not result["executed"]:
        return {
            "status": "error",
            "message": f"Exit order was REJECTED by Fyers — position {position_id} may still be open. "
                       f"Reason: {result['result'].get('message', 'unknown')}",
            "result": result,
        }

    return {"status": "ok", "message": f"Position {position_id} exited ({result['mode']} mode).", "pnl": round(pnl, 2), "result": result}


@app.get("/trade-log/stats")
async def trade_log_stats():
    return {"status": "ok", **get_trade_log_stats()}


@app.get("/trade-log/detailed-stats")
async def trade_log_detailed_stats(index_id: str = None, start_date: str = None, end_date: str = None):
    """
    Richer stats for the Trade Statistics UI — profit factor, best/
    worst trade, max drawdown, and the full equity curve (cumulative
    P&L over time). Accepts the same filters as the trade list itself
    so the frontend's Filter controls stay in sync between the table
    and the stats/chart above it.
    """
    result = get_trade_log_detailed_stats(
        index_id=index_id.upper() if index_id else None, start_date=start_date, end_date=end_date,
    )
    return {"status": "ok", **result}


@app.get("/trade-log/export")
async def trade_log_export(index_id: str = None, start_date: str = None, end_date: str = None):
    """
    Returns the same filtered trade list as CSV text (not JSON), so the
    frontend can trigger a direct file download. Filters match
    /trade-log/detailed-stats exactly, so exporting always matches
    whatever the person is currently looking at on screen.
    """
    from fastapi.responses import PlainTextResponse
    import csv
    import io

    conn_query_result = get_trade_log_detailed_stats(
        index_id=index_id.upper() if index_id else None, start_date=start_date, end_date=end_date,
    )
    # Re-fetch full row detail (detailed-stats only returns equity_curve
    # points, not every column) — reuse get_trade_log with the same
    # filtering logic via a direct query for full fidelity in the export.
    from config.db import get_connection
    conn = get_connection()
    query = "SELECT * FROM trade_log WHERE 1=1"
    params = []
    if index_id:
        query += " AND index_id = ?"
        params.append(index_id.upper())
    if start_date:
        query += " AND exit_time >= ?"
        params.append(start_date)
    if end_date:
        query += " AND exit_time <= ?"
        params.append(end_date)
    query += " ORDER BY exit_time ASC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()

    if not rows:
        return PlainTextResponse("No trades match the selected filters.", media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(output.getvalue(), media_type="text/csv",
                              headers={"Content-Disposition": "attachment; filename=trade_log_export.csv"})


@app.post("/trade-log/clear")
async def trade_log_clear(confirm: bool = False):
    """
    Deletes closed-trade history (trade_log + closed positions). Any
    currently OPEN position is left untouched. Requires confirm=true
    explicitly — this is destructive and cannot be undone.
    """
    if not confirm:
        return {"status": "error", "message": "Pass confirm=true to actually clear history. This cannot be undone."}
    result = clear_trade_history()
    return {"status": "ok", **result, "message": "Closed trade history cleared. Open positions were preserved."}


@app.post("/paper/enter/{index_id}")
async def paper_enter(index_id: str, available_capital: float = 100000,
                       risk_per_trade_pct: float = 2.0, hard_sl_pct: float = 27.0,
                       min_confidence: float = 0.5):
    """
    Manual-trigger paper entry (Step 36) — evaluates the confluence
    gate, selects a strike, sizes the position, and opens a paper
    position if everything checks out. Entry is NOT automatic yet
    (that's a deliberate next step, not shipped in this batch) — this
    endpoint lets you test the full pipeline safely before we wire it
    to fire on its own.
    """
    index_id = index_id.upper()

    sizing = await position_size_signal(index_id, available_capital=available_capital,
                                          risk_per_trade_pct=risk_per_trade_pct, hard_sl_pct=hard_sl_pct,
                                          min_confidence=min_confidence)
    if sizing.get("lots", 0) < 1:
        return {"status": "ok", "opened": False, "note": sizing.get("note", "position sizing returned 0 lots")}

    selected_strike = sizing["selected_strike"]
    direction = sizing["direction"]
    lots = sizing["lots"]
    lot_size = sizing["lot_size"]

    entry_result = execute_paper_entry(index_id, direction, selected_strike, lots, lot_size)
    return {"status": "ok", **entry_result}


# ---------------- Config Management (risk params + trading mode) ----------------

@app.get("/config/indices")
async def get_all_indices_config_endpoint():
    """Returns every index's full config (risk params, mode, capital allocation) in one call."""
    configs = get_all_index_configs()
    for idx in INDICES:
        if configs[idx]:
            configs[idx]["capital_rupees"] = get_index_capital(idx)
    return {"status": "ok", "indices": configs, "account": get_account_config()}


@app.get("/config/indices/{index_id}")
async def get_index_config_endpoint(index_id: str):
    cfg = get_index_config(index_id.upper())
    if cfg is None:
        return {"status": "error", "message": f"Unknown index '{index_id}'"}
    cfg["capital_rupees"] = get_index_capital(index_id.upper())
    return {"status": "ok", "config": cfg}


class IndexRiskParamsUpdate(BaseModel):
    hard_sl_pct: float | None = None
    time_stop_minutes: int | None = None
    tsl_activation_pct: float | None = None
    tsl_base_trail_pct: float | None = None
    tsl_k: float | None = None
    tsl_floor_pct: float | None = None
    risk_per_trade_pct: float | None = None
    min_confluence_confidence: float | None = None
    max_entry_iv_rank: float | None = None


@app.post("/config/indices/{index_id}/risk")
async def update_index_risk_endpoint(index_id: str, updates: IndexRiskParamsUpdate):
    non_null_updates = {k: v for k, v in updates.dict().items() if v is not None}
    new_config = update_index_risk_params(index_id.upper(), non_null_updates)
    if new_config is None:
        return {"status": "error", "message": f"Unknown index '{index_id}'"}
    return {"status": "ok", "config": new_config}


class IndexModeUpdate(BaseModel):
    mode: str  # "paper" | "live" | "both" | "off"


@app.post("/config/indices/{index_id}/mode")
async def update_index_mode_endpoint(index_id: str, body: IndexModeUpdate):
    try:
        new_config = update_index_mode(index_id.upper(), body.mode)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    if new_config is None:
        return {"status": "error", "message": f"Unknown index '{index_id}'"}
    return {"status": "ok", "config": new_config}


@app.post("/config/indices/{index_id}/reset")
async def reset_index_config_endpoint(index_id: str):
    new_config = reset_index_config(index_id.upper())
    if new_config is None:
        return {"status": "error", "message": f"Unknown index '{index_id}'"}
    return {"status": "ok", "config": new_config, "message": f"{index_id.upper()} reset to defaults."}


@app.post("/config/indices/reset-all")
async def reset_all_indices_endpoint():
    return {"status": "ok", "indices": reset_all_index_configs(), "message": "All indices reset to defaults."}


class CapitalAllocationUpdate(BaseModel):
    allocations: dict[str, float]  # {"NIFTY50": 100} -- Nifty-only app, BankNifty/FinNifty removed


@app.post("/config/capital-allocation")
async def update_capital_allocation_endpoint(body: CapitalAllocationUpdate):
    result = update_capital_allocations(body.allocations)
    return result


@app.get("/config/account")
async def get_account_config_endpoint():
    return {"status": "ok", "config": get_account_config()}


class AccountConfigUpdate(BaseModel):
    total_available_capital: float | None = None
    auto_entry_enabled: bool | None = None
    auto_entry_poll_seconds: int | None = None
    daily_loss_limit_pct: float | None = None
    consecutive_loss_throttle: int | None = None
    consecutive_loss_size_multiplier: float | None = None
    max_concurrent_positions: int | None = None
    weekly_drawdown_cap_pct: float | None = None
    strikes_tracked_per_side: int | None = None
    live_trading_master_enabled: bool | None = None
    # Master safety switch for REAL order placement, separate from and
    # in addition to each index's own mode setting (see
    # /config/indices/{index_id}/mode above). BOTH must be on before
    # any real order is ever placed — see
    # engine/execution/trade_dispatcher.py's is_live_trading_active_for.


@app.post("/config/account")
async def update_account_config_endpoint(updates: AccountConfigUpdate):
    non_null_updates = {k: v for k, v in updates.dict().items() if v is not None}
    new_config = update_account_config(non_null_updates)
    return {"status": "ok", "config": new_config}


# ---------------- Backtest Engine ----------------

@app.get("/backtest/run/{index_id}")
async def backtest_run(index_id: str, candle_limit: int = 2000, initial_capital: float = 100000,
                        risk_per_trade_pct: float = None, hard_sl_pct: float = None,
                        time_stop_minutes: int = None, tsl_activation_pct: float = None,
                        tsl_base_trail_pct: float = None, tsl_k: float = None, tsl_floor_pct: float = None,
                        min_confidence: float = None, walk_forward: bool = False, save_to_history: bool = True):
    """
    PRD §6 — replays stored historical candles through the same gate +
    risk engine logic used live. See engine/backtest/engine.py for the
    documented limitation on OFI/PCR (not backtestable — no historical
    depth/chain data retained) and the option-premium approximation.

    Any override param left as None falls back to the current LIVE risk
    config — this is the "test first, override later" flow: by default
    a backtest reflects exactly what live trading would do right now,
    but you can pass explicit overrides to experiment with different
    settings before deciding whether to apply them.

    walk_forward=True splits the candle history 70/30 (train/test) and
    runs both segments separately, so you can see if performance holds
    up out-of-sample rather than just curve-fitting to the whole history.
    """
    index_id = index_id.upper()
    candles = get_recent_candles(index_id, limit=candle_limit)

    if len(candles) < 150:
        return {"status": "error", "message": f"Not enough candle history ({len(candles)} candles) — run /backfill/{index_id} first, or wait for more live data to accumulate."}

    live_config = get_index_config(index_id)
    if live_config is None:
        return {"status": "error", "message": f"Unknown index '{index_id}'"}
    live_risk = live_config["risk_params"]
    effective_params = {
        "initial_capital": initial_capital,
        "risk_per_trade_pct": risk_per_trade_pct if risk_per_trade_pct is not None else live_risk["risk_per_trade_pct"],
        "hard_sl_pct": hard_sl_pct if hard_sl_pct is not None else live_risk["hard_sl_pct"],
        "time_stop_minutes": time_stop_minutes if time_stop_minutes is not None else live_risk["time_stop_minutes"],
        "tsl_activation_pct": tsl_activation_pct if tsl_activation_pct is not None else live_risk["tsl_activation_pct"],
        "tsl_base_trail_pct": tsl_base_trail_pct if tsl_base_trail_pct is not None else live_risk["tsl_base_trail_pct"],
        "tsl_k": tsl_k if tsl_k is not None else live_risk["tsl_k"],
        "tsl_floor_pct": tsl_floor_pct if tsl_floor_pct is not None else live_risk["tsl_floor_pct"],
        "min_confidence": min_confidence if min_confidence is not None else live_risk["min_confluence_confidence"],
    }

    if not walk_forward:
        result = run_backtest(candles, index_id, **effective_params)
        if save_to_history:
            run_id = insert_backtest_run(index_id, "full_history", effective_params, result.get("summary"))
            result["backtest_run_id"] = run_id
        return {"status": "ok", "index_id": index_id, "mode": "full_history", "candle_count": len(candles),
                "params_used": effective_params, **result}

    split_point = int(len(candles) * 0.7)
    train_candles = candles[:split_point]
    test_candles = candles[split_point:]

    if len(test_candles) < 150:
        return {"status": "error", "message": "Not enough candles to walk-forward split (need at least ~215 total for a meaningful 70/30 split)."}

    train_result = run_backtest(train_candles, index_id, **effective_params)
    test_result = run_backtest(test_candles, index_id, **effective_params)

    if save_to_history:
        run_id = insert_backtest_run(index_id, "walk_forward_test", effective_params, test_result.get("summary"))
        test_result["backtest_run_id"] = run_id

    return {
        "status": "ok", "index_id": index_id, "mode": "walk_forward",
        "train_candle_count": len(train_candles), "test_candle_count": len(test_candles),
        "params_used": effective_params, "train": train_result, "test": test_result,
        "note": "Compare train vs test summary stats — if test performance is much worse than train, the strategy may be overfit to the training period.",
    }


@app.get("/backtest/history")
async def backtest_history(index_id: str = None, limit: int = 20):
    runs = get_backtest_runs(index_id.upper() if index_id else None, limit=limit)
    return {"status": "ok", "count": len(runs), "runs": runs}


class ApplyBacktestParams(BaseModel):
    run_id: int
    index_id: str
    params: dict


@app.post("/backtest/apply")
async def backtest_apply(body: ApplyBacktestParams):
    """
    Promotes a backtest run's parameters into that SPECIFIC INDEX's live
    risk config — this is the "if satisfied, apply it" step. The
    frontend is expected to show a diff (current live values -> these
    new values) before calling this, so nothing changes silently.
    """
    applicable_keys = {
        "hard_sl_pct", "time_stop_minutes", "tsl_activation_pct", "tsl_base_trail_pct",
        "tsl_k", "tsl_floor_pct", "risk_per_trade_pct", "min_confidence",
    }
    # backtest params use "min_confidence", live config uses "min_confluence_confidence" — reconcile the name
    updates = {k: v for k, v in body.params.items() if k in applicable_keys}
    if "min_confidence" in updates:
        updates["min_confluence_confidence"] = updates.pop("min_confidence")

    new_config = update_index_risk_params(body.index_id.upper(), updates)
    if new_config is None:
        return {"status": "error", "message": f"Unknown index '{body.index_id}'"}
    mark_backtest_run_applied(body.run_id)
    return {"status": "ok", "config": new_config, "message": f"Backtest run #{body.run_id}'s settings applied to {body.index_id.upper()}'s live config."}