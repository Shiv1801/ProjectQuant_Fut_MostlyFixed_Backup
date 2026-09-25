"""
Diagnostic: Why Zero Trades in 2021-2024? — walks the same 5-year
candle history used by vwap_5yr_detailed_log.py and reports, YEAR BY
YEAR, how many candles pass each stage of the pipeline:
  1) Gate direction fires at all (long_call or long_put, not None)
  2) ...AND confidence >= 0.95 (your live threshold)
  3) ...AND chart structure confirms
  4) ...AND the VWAP filter agrees with the gate's direction

This isolates EXACTLY which stage collapses to zero for the missing
years, rather than guessing.

Usage:
    python scripts/diagnose_zero_trades.py

Requires the 5-year cache already built by vwap_5yr_detailed_log.py
(vwap_5yr_cache/nifty_5yr_candles.json) and uvicorn running.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import requests

from engine.signal_layer.momentum.hurst import compute_hurst_exponent
from engine.signal_layer.momentum.zscore import compute_zscore
from engine.confluence_gate.quant_score import compute_quant_confidence
from engine.confluence_gate.chart_structure import confirm_chart_structure

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")
API_BASE = "http://127.0.0.1:8001"
HURST_WINDOW = 100
ZSCORE_WINDOW = 20


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def fetch_live_nifty_config() -> dict:
    try:
        resp = requests.get(f"{API_BASE}/config/indices/NIFTY50", timeout=5)
        payload = resp.json()
        return payload["config"]
    except requests.exceptions.RequestException as e:
        log(f"ERROR: could not reach the running app at {API_BASE}: {e}")
        sys.exit(1)


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run vwap_5yr_detailed_log.py first (even just to build the cache).")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        candles = json.load(f)
    log(f"Loaded {len(candles)} cached candles.")
    return candles


def compute_vwap_series(candles: list[dict]) -> list[float | None]:
    n = len(candles)
    vwap = [None] * n
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    current_day = None
    for i, c in enumerate(candles):
        candle_day = datetime.fromtimestamp(c["epoch"], tz=IST).date()
        if candle_day != current_day:
            current_day = candle_day
            cumulative_pv = 0.0
            cumulative_vol = 0.0
        volume = c.get("volume", 0) or 0
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        cumulative_pv += typical_price * volume
        cumulative_vol += volume
        vwap[i] = (cumulative_pv / cumulative_vol) if cumulative_vol > 0 else None
    return vwap


def main():
    nifty_config = fetch_live_nifty_config()
    min_confidence = nifty_config["risk_params"]["min_confluence_confidence"]
    log(f"Live min_confluence_confidence: {min_confidence}")

    candles = load_candles()
    closes = np.array([c["close"] for c in candles])
    n = len(closes)

    log("Computing VWAP...")
    vwap_series = compute_vwap_series(candles)

    log("Walking the full 5-year dataset, tracking funnel stage-by-stage per year...")
    log("(this recomputes signals fresh, same as the main script -- will take a couple minutes)")

    # Per-year counters
    stage_counts = defaultdict(lambda: {"direction_fired": 0, "confidence_passed": 0,
                                         "structure_confirmed": 0, "vwap_agreed": 0,
                                         "confidence_values_when_fired": []})

    for i in range(HURST_WINDOW, n):
        year = datetime.fromtimestamp(candles[i]["epoch"], tz=IST).year

        window = closes[max(0, i - HURST_WINDOW):i]
        hurst_result = compute_hurst_exponent(window.tolist())
        zscore_result = compute_zscore(window[-ZSCORE_WINDOW:].tolist(), window=ZSCORE_WINDOW)
        model_outputs = {"hurst": hurst_result, "zscore": zscore_result, "ofi": {"ofi": None}, "pcr": {"pcr": None}}
        quant_result = compute_quant_confidence(model_outputs)
        direction = quant_result.get("direction")
        confidence = quant_result.get("confidence", 0.0)

        if direction is None:
            continue
        stage_counts[year]["direction_fired"] += 1
        stage_counts[year]["confidence_values_when_fired"].append(confidence)

        if confidence < min_confidence:
            continue
        stage_counts[year]["confidence_passed"] += 1

        chart_slice = candles[max(0, i - HURST_WINDOW):i + 1]
        structure_result = confirm_chart_structure(chart_slice, direction)
        if not structure_result.get("confirmed", False):
            continue
        stage_counts[year]["structure_confirmed"] += 1

        if vwap_series[i] is None:
            continue
        vwap_signal = "bullish" if closes[i] > vwap_series[i] else "bearish"
        gate_signal = "bullish" if direction == "long_call" else "bearish"
        if vwap_signal != gate_signal:
            continue
        stage_counts[year]["vwap_agreed"] += 1

        if (i - HURST_WINDOW) % 50000 == 0:
            log(f"  ...processed through year {year}, candle {i}/{n}")

    print("\n" + "=" * 90)
    print(f"FUNNEL BY YEAR (live min_confluence_confidence = {min_confidence})")
    print("=" * 90)
    print(f"{'Year':<6} {'Direction Fired':<17} {'Confidence>=Threshold':<23} {'Structure OK':<14} {'VWAP Agreed':<12} {'Avg Confidence (when fired)'}")
    print("-" * 90)
    for year in sorted(stage_counts.keys()):
        s = stage_counts[year]
        avg_conf = (sum(s["confidence_values_when_fired"]) / len(s["confidence_values_when_fired"])
                    if s["confidence_values_when_fired"] else 0)
        max_conf = max(s["confidence_values_when_fired"]) if s["confidence_values_when_fired"] else 0
        print(f"{year:<6} {s['direction_fired']:<17} {s['confidence_passed']:<23} {s['structure_confirmed']:<14} "
              f"{s['vwap_agreed']:<12} avg={avg_conf:.3f}, max={max_conf:.3f}")

    print("\n" + "-" * 90)
    print("READ THIS COLUMN BY COLUMN:")
    print("If 'Confidence>=Threshold' is 0 for a year even though 'Direction Fired' is high,")
    print(f"the confidence scores that year never reached your {min_confidence} threshold --")
    print("the GATE ITSELF is the bottleneck, not VWAP or chart structure.")
    print("\nIf 'Confidence>=Threshold' is healthy but 'VWAP Agreed' collapses to 0,")
    print("VWAP is the bottleneck instead.")


if __name__ == "__main__":
    main()
