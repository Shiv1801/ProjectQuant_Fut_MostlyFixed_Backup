"""
STANDALONE version of the 4-way gate comparison (Quant only / Quant+Chart /
Quant+VWAP / All together) using real Black-Scholes pricing.

This does NOT require api/main.py to be modified or uvicorn to be running
at all -- it imports run_backtest_bs and reads candles/config directly,
the same underlying code the API endpoint would call, just without going
through HTTP. This is the safer option since you specifically don't want
to touch your currently-running app's files.

Nothing here writes to your database, your live INDEX_CONFIG, or any file
your running app depends on -- get_recent_candles() and get_index_config()
are read-only lookups.

Uses the SAME engine/backtest/engine_bs.py and engine/backtest/bs_pricer.py
files already added to your project (both are new, additive files -- not
edits to anything you were already running).

Run from the project root (uvicorn does NOT need to be running for this):

    python -m scripts.backtest_gate_legs_nifty_bs_standalone
"""
import csv
import sys
import os

sys.path.insert(0, os.getcwd())

from config.db import get_recent_candles, get_candle_count
from config.trading_config import get_index_config
from engine.backtest.engine_bs import run_backtest_bs

INDEX_ID = "NIFTY50"
CANDLE_LIMIT = 200_000

GATE_COMBINATIONS = [
    ("quant_only",    "Quant only",                       False, False),
    ("quant_chart",   "Quant + Chart",                     True,  False),
    ("quant_vwap",    "Quant + VWAP",                      False, True),
    ("all_together",  "All together (Quant+Chart+VWAP)",   True,  True),
]

TRADE_LOG_FIELDS = [
    "trade_num", "entry_time", "exit_time", "direction", "option_type", "strike",
    "entry_spot", "exit_spot", "entry_premium", "exit_premium",
    "entry_sigma_annualized_pct", "days_to_expiry_at_entry",
    "lots", "lot_size", "exit_reason", "pnl", "pnl_pct", "peak_profit_pct",
    "hold_minutes", "capital_before", "capital_after",
    "quant_confidence", "chart_confirmed", "vwap_confirmed",
]


def write_trade_log(slug: str, trades: list[dict]) -> str:
    filename = f"backtest_trades_{slug}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writeheader()
        for t in trades:
            writer.writerow({k: t.get(k) for k in TRADE_LOG_FIELDS})
    return filename


def main():
    candle_count = get_candle_count(INDEX_ID)
    print(f"Candle coverage for {INDEX_ID}: {candle_count}")
    if candle_count < 50_000:
        print(f"WARNING: only {candle_count} candles -- short of ~2 years (~187,500 candles). "
              f"Results reflect whatever history is actually stored, not necessarily a full 2-year window.\n")

    candles = get_recent_candles(INDEX_ID, limit=CANDLE_LIMIT)
    print(f"Loaded {len(candles)} candles for backtest.\n")

    live_config = get_index_config(INDEX_ID)
    if live_config is None:
        print(f"ERROR: no config found for {INDEX_ID}")
        return
    live_risk = live_config["risk_params"]

    base_params = {
        "initial_capital": 30000,
        "risk_per_trade_pct": live_risk["risk_per_trade_pct"],
        "hard_sl_pct": live_risk["hard_sl_pct"],
        "time_stop_minutes": live_risk["time_stop_minutes"],
        "tsl_activation_pct": live_risk["tsl_activation_pct"],
        "tsl_base_trail_pct": live_risk["tsl_base_trail_pct"],
        "tsl_k": live_risk["tsl_k"],
        "tsl_floor_pct": live_risk["tsl_floor_pct"],
        "min_confidence": live_risk["min_confluence_confidence"],
        "log_all_trades": True,
    }
    print(f"Using your CURRENT LIVE risk params for every run below (only the gate combination changes):")
    for k, v in base_params.items():
        if k != "log_all_trades":
            print(f"  {k}: {v}")
    print()

    summary_rows = []
    for slug, label, req_chart, req_vwap in GATE_COMBINATIONS:
        print(f"[{label}] require_chart_confirmation={req_chart}, require_vwap={req_vwap} ... ", end="", flush=True)
        payload = run_backtest_bs(
            candles, INDEX_ID, require_chart_confirmation=req_chart, require_vwap=req_vwap, **base_params,
        )

        summary = payload.get("summary") or {}
        diagnostics = payload.get("diagnostics") or {}
        trades = payload.get("trades") or []

        if payload.get("note", "").startswith("insufficient") or summary is None:
            print(f"ERROR: {payload.get('note')}")
            summary_rows.append({"combination": label, "error": payload.get("note")})
            continue

        csv_file = write_trade_log(slug, trades)
        print(f"OK — {summary.get('trade_count', 0)} trades, log written to {csv_file}")

        summary_rows.append({
            "combination": label,
            "trade_count": summary.get("trade_count"),
            "win_rate": summary.get("win_rate"),
            "return_pct": summary.get("return_pct"),
            "total_pnl": summary.get("total_pnl"),
            "final_capital": summary.get("final_capital"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "avg_win_pct": summary.get("avg_win_pct"),
            "avg_loss_pct": summary.get("avg_loss_pct"),
            "avg_hold_minutes": summary.get("avg_hold_minutes"),
            "exit_reason_breakdown": summary.get("exit_reason_breakdown"),
            "zero_volume_pct": diagnostics.get("zero_volume_pct"),
            "csv_file": csv_file,
        })

    print("\n" + "=" * 120)
    print(f"{'Combination':<36} {'Trades':<7} {'WinRate%':<9} {'Return%':<9} {'FinalCap':<11} "
          f"{'MaxDD%':<8} {'AvgWin%':<8} {'AvgLoss%':<9} {'AvgHold(m)':<11}")
    print("-" * 120)
    for r in summary_rows:
        if "error" in r:
            print(f"{r['combination']:<36} ERROR: {r['error']}")
            continue
        win_pct = f"{r['win_rate']*100:.1f}" if r['win_rate'] is not None else "—"
        print(f"{r['combination']:<36} {str(r['trade_count']):<7} {win_pct:<9} {str(r['return_pct']):<9} "
              f"{str(r['final_capital']):<11} {str(r['max_drawdown_pct']):<8} {str(r['avg_win_pct']):<8} "
              f"{str(r['avg_loss_pct']):<9} {str(r['avg_hold_minutes']):<11}")
    print("=" * 120)

    print("\nExit reason breakdown per combination:")
    for r in summary_rows:
        if "error" not in r:
            print(f"  {r['combination']}: {r['exit_reason_breakdown']}  (zero-volume candles: {r['zero_volume_pct']}%)")

    print("\nFull per-trade logs written to:")
    for r in summary_rows:
        if "error" not in r:
            print(f"  {r['csv_file']}  ({r['trade_count']} trades)")

    print("\nIMPORTANT: if 'zero-volume candles' is high for a VWAP-required combination, that leg was "
          "mostly SKIPPED (no real volume to check), not genuinely enforced -- confluence_gate/gate.py "
          "documents that 2021-2024 NIFTY historical data has zero recorded volume in this app's data "
          "source (a known Fyers data-provider limitation). In that case 'Quant+VWAP'/'All together' may "
          "resemble their non-VWAP counterparts simply because VWAP had nothing to check.")

    print("\nAccuracy note: strike, expiry, and spot price in every trade above are REAL. Volatility "
          "(sigma) is estimated from realized historical volatility of the same candles, since no "
          "historical implied-volatility data is stored anywhere in this app -- standard practice "
          "absent stored option-chain history, but a proxy, not the exact historical IV market makers "
          "were quoting on those dates.")


if __name__ == "__main__":
    main()
