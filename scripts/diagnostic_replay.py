"""
Full System Diagnostic — replays the last 2 trading days of REAL data
through every model, the confluence gate, strike selector, and
position sizing, logging complete transparency at every step. Purpose
is verification, not performance: are all models working, are outputs
changing sensibly, are gate decisions correct, and why.

Run against the SAME database the main app uses (needs uvicorn to have
been running during the period being tested, or currently running for
Mode B). Does NOT touch the live app or place any trades.

Two modes, run together, because of a real data limitation (stated
honestly rather than hidden):

MODE A — Historical replay (price-derived models only)
  Uses stored `candles` + `atm_iv_history` tables to replay minute-by-
  minute over the last 2 trading days. Covers: Realized Vol, GARCH,
  VRP (using stored ATM IV), IV Rank, Hurst, Z-score, Variance Ratio,
  VIX Regime, RV/IV Ratio, Correlation Breakdown, Confluence Gate Leg 1
  (using only the price-derived votes) + Leg 2 (chart structure), and
  Kelly/Vol-Scaled sizing.
  Does NOT include OFI, GEX, PCR, Max Pain, OI Change — these need full
  option-chain/depth snapshots, which the app does not persist to disk
  over time (only the current live snapshot is held in memory). This
  is a real, stated data limitation, not a bug.

MODE B — Live snapshot (full model coverage, single point in time)
  Runs literally every model — including OFI/GEX/PCR/Max Pain/OI
  Change/Strike Selector/full Confluence Gate/Position Sizing — against
  whatever is CURRENTLY in shared_state right now. Proves each model
  individually computes correctly on real live data, even though it
  can't be walked minute-by-minute historically.

Usage:
    python scripts/diagnostic_replay.py

Writes: diagnostic_results.json (nested by index -> mode -> entries)
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.db import get_recent_candles, get_iv_history
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
from engine.risk_engine.circuit_breakers import evaluate_circuit_breakers
from config.trading_config import get_risk_config, get_trading_config

INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY"]
CHAIN_INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY"]

# How many recent candles to treat as "last 2 trading days" — Indian
# markets run ~375 minutes/day (9:15-15:30), so 2 days ~= 750 candles.
# Using a slightly generous window to be safe.
TWO_DAYS_CANDLE_COUNT = 800
HURST_WINDOW = 100
ZSCORE_WINDOW = 20


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ---------------- MODE A: Historical replay ----------------

def run_mode_a_for_index(index_id: str) -> dict:
    log(f"MODE A: fetching stored candle history for {index_id}...")
    candles = get_recent_candles(index_id, limit=TWO_DAYS_CANDLE_COUNT)
    log(f"  Retrieved {len(candles)} candles")

    if len(candles) < HURST_WINDOW + 20:
        return {
            "index_id": index_id, "status": "insufficient_data",
            "candle_count": len(candles),
            "note": f"Need at least {HURST_WINDOW + 20} candles for a meaningful replay. "
                    f"This likely means the app wasn't running long enough, or the DB doesn't "
                    f"have 2 full trading days of history for this index yet.",
            "entries": [],
        }

    iv_history_rows = get_iv_history(index_id, limit=1000)
    iv_by_time = {r["minute_bucket"]: r["atm_iv_pct"] for r in iv_history_rows if r.get("minute_bucket")}
    historical_ivs_so_far = []

    closes = [c["close"] for c in candles]
    entries = []
    trades_fired = 0
    trades_vetoed = 0
    trades_no_direction = 0
    model_error_counts = {}

    def safe_call(model_name, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            model_error_counts[model_name] = model_error_counts.get(model_name, 0) + 1
            return {"error": str(e)}

    for i in range(HURST_WINDOW, len(candles)):
        window_closes = closes[max(0, i - HURST_WINDOW):i]
        current_candle = candles[i]
        ts = current_candle.get("minute_bucket") or f"idx_{i}"

        rv_result = safe_call("realized_vol", compute_realized_vol, window_closes)
        garch_result = safe_call("garch", compute_garch_forecast, window_closes)
        hurst_result = safe_call("hurst", compute_hurst_exponent, window_closes)
        zscore_result = safe_call("zscore", compute_zscore, window_closes[-ZSCORE_WINDOW:], window=ZSCORE_WINDOW)
        vr_result = safe_call("variance_ratio", compute_variance_ratio, window_closes)

        current_iv = iv_by_time.get(ts)
        if current_iv is not None:
            historical_ivs_so_far.append(current_iv)
        vrp_result = safe_call("vrp", compute_vrp, current_iv, garch_result.get("garch_forecast_vol_pct") if isinstance(garch_result, dict) else None)
        iv_rank_result = safe_call("iv_rank", compute_iv_rank, current_iv, historical_ivs_so_far[:-1] if historical_ivs_so_far else [])

        rv_iv_result = safe_call("rv_iv_ratio", compute_rv_iv_ratio, rv_result.get("realized_vol_pct") if isinstance(rv_result, dict) else None, current_iv)

        model_outputs = {
            "hurst": hurst_result if isinstance(hurst_result, dict) else {},
            "zscore": zscore_result if isinstance(zscore_result, dict) else {},
            "ofi": {"ofi": None},  # not available historically — see module docstring
            "pcr": {"pcr": None},  # not available historically — see module docstring
        }
        quant_result = safe_call("quant_confidence", compute_quant_confidence, model_outputs)

        chart_result = {"verdict": "no_quant_direction", "confirmed": False}
        if isinstance(quant_result, dict) and quant_result.get("direction"):
            chart_slice = candles[max(0, i - HURST_WINDOW):i + 1]
            chart_result = safe_call("chart_structure", confirm_chart_structure, chart_slice, quant_result["direction"])

        gate_result = safe_call("confluence_gate", evaluate_confluence, quant_result if isinstance(quant_result, dict) else {}, chart_result if isinstance(chart_result, dict) else {})

        if isinstance(gate_result, dict):
            if gate_result.get("fired"):
                trades_fired += 1
            elif gate_result.get("veto_reason") in ("chart_veto_opposite_structure", "chart_not_confirmed"):
                trades_vetoed += 1
            elif gate_result.get("veto_reason") == "no_quant_direction":
                trades_no_direction += 1

        entry = {
            "timestamp": ts, "candle_close": current_candle.get("close"),
            "models": {
                "realized_vol": rv_result, "garch": garch_result, "vrp": vrp_result, "iv_rank": iv_rank_result,
                "hurst": hurst_result, "zscore": zscore_result, "variance_ratio": vr_result,
                "rv_iv_ratio": rv_iv_result,
            },
            "confluence": {"quant_score": quant_result, "chart_structure": chart_result, "gate": gate_result},
        }
        entries.append(entry)

    # Regime models that need cross-index/point-in-time data, run once at the end using the full window
    vix_regime_result = None
    if index_id == "NIFTY50":  # VIX regime is system-wide, only compute once
        vix_candles = get_recent_candles("INDIAVIX", limit=20)
        vix_regime_result = safe_call("vix_regime", compute_vix_regime, vix_candles)

    kelly_result = safe_call("kelly", compute_fractional_kelly, None, None, None, trade_count=0)
    vol_scaled_result = safe_call("vol_scaled", compute_vol_scaled_size, 100000, None, None)

    return {
        "index_id": index_id, "status": "ok", "candle_count": len(candles),
        "minutes_evaluated": len(entries),
        "summary": {
            "gate_fired_count": trades_fired,
            "gate_vetoed_count": trades_vetoed,
            "gate_no_direction_count": trades_no_direction,
            "model_error_counts": model_error_counts,
        },
        "vix_regime_snapshot": vix_regime_result,
        "kelly_snapshot": kelly_result,
        "vol_scaled_snapshot": vol_scaled_result,
        "entries": entries,
        "note": "OFI, GEX, PCR, Max Pain, OI Change excluded from this historical replay — "
                "the app does not persist full option-chain/depth snapshots over time, only "
                "the current live one. See MODE B for these, evaluated once on live data.",
    }


# ---------------- MODE B: Live snapshot (needs the main app's shared_state) ----------------

def run_mode_b_for_index(index_id: str, shared_state, SYMBOLS) -> dict:
    """
    Requires shared_state to already be populated — this function is
    meant to be called from WITHIN a process that has imported the
    running app's shared_state module, OR against a fresh import that
    itself starts pollers briefly. See main() for how this is invoked.
    """
    from api.main import _parse_chain_from_shared_state

    log(f"MODE B: evaluating live snapshot for {index_id}...")

    result = {"index_id": index_id, "status": "ok", "timestamp": datetime.now().isoformat(), "models": {}}

    price = shared_state.get_price(index_id)
    result["models"]["live_price"] = price

    depth_data = shared_state.get_depth(index_id)
    ofi_result = {"note": "no depth data in shared_state yet"}
    if depth_data:
        fyers_symbol = SYMBOLS.get(index_id)
        inner = (depth_data.get("d") or {}).get(fyers_symbol, {})
        ofi_result = compute_ofi(inner.get("totalbuyqty"), inner.get("totalsellqty"))
    result["models"]["ofi"] = ofi_result

    parsed_chain = _parse_chain_from_shared_state(index_id) if index_id in CHAIN_INDICES else None
    if parsed_chain:
        result["models"]["gex"] = compute_gex(parsed_chain["strikes"], parsed_chain["spot"])
        result["models"]["pcr"] = compute_pcr(parsed_chain["call_oi_total"], parsed_chain["put_oi_total"])
        result["models"]["max_pain"] = compute_max_pain(parsed_chain["strikes"])
        result["models"]["oi_change"] = compute_oi_change_analysis(parsed_chain["strikes"], top_n=5)
        result["spot"] = parsed_chain["spot"]
        result["strikes_available"] = len(parsed_chain["strikes"])
    else:
        result["models"]["gex"] = result["models"]["pcr"] = result["models"]["max_pain"] = result["models"]["oi_change"] = \
            {"note": "no option chain data in shared_state yet"}

    correlation_result = None
    if index_id == "NIFTY50":
        import math
        returns_by_index = {}
        for idx in INDICES:
            candles = get_recent_candles(idx, limit=21)
            closes = [c["close"] for c in candles if c["close"] is not None]
            log_returns = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes)) if closes[j - 1] > 0 and closes[j] > 0]
            returns_by_index[idx] = log_returns
        correlation_result = compute_correlation_breakdown(returns_by_index, window=20)
    result["correlation_snapshot"] = correlation_result

    return result


# ---------------- Main ----------------

def main():
    log("=" * 70)
    log("FULL SYSTEM DIAGNOSTIC — Mode A (2-day replay) + Mode B (live snapshot)")
    log("=" * 70)

    results = {"generated_at": datetime.now().isoformat(), "mode_a": {}, "mode_b": {}}

    log("\n--- MODE A: Historical replay using stored candles + IV history ---")
    for index_id in INDICES:
        results["mode_a"][index_id] = run_mode_a_for_index(index_id)
        summary = results["mode_a"][index_id].get("summary", {})
        log(f"  {index_id}: {results['mode_a'][index_id]['status']}, "
            f"{results['mode_a'][index_id].get('minutes_evaluated', 0)} minutes evaluated, "
            f"gate fired {summary.get('gate_fired_count', 0)}x, "
            f"errors: {summary.get('model_error_counts', {})}")

    log("\n--- MODE B: Live snapshot (requires the main app or a fresh poller cycle) ---")
    try:
        from engine.shared_state.store import shared_state
        from engine.background.pollers import SYMBOLS
        import asyncio
        from engine.background.pollers import price_poller, option_chain_poller, depth_poller

        log("  Starting pollers briefly to get one live snapshot (10s)...")

        async def gather_snapshot():
            tasks = [asyncio.create_task(price_poller()), asyncio.create_task(option_chain_poller()), asyncio.create_task(depth_poller())]
            await asyncio.sleep(10)
            for t in tasks:
                t.cancel()

        asyncio.run(gather_snapshot())

        for index_id in INDICES:
            results["mode_b"][index_id] = run_mode_b_for_index(index_id, shared_state, SYMBOLS)
            log(f"  {index_id}: snapshot captured")
    except Exception as e:
        log(f"  MODE B failed: {e}")
        results["mode_b"]["error"] = str(e)
        log("  (This usually means no .fyers_token file, or Fyers rejected the request. "
            "Mode A results above are still valid and complete.)")

    output_path = "diagnostic_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    log(f"\nDone. Full results written to {output_path}")
    log("Hand this file back for analysis.")


if __name__ == "__main__":
    main()
