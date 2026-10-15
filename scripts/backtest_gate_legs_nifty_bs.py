"""
CORRECTED 4-way gate comparison (Quant only / Quant+Chart / Quant+VWAP /
All together) on NIFTY50, using REAL Black-Scholes option pricing instead
of the old flat-approx_entry_premium=60 placeholder that produced a
fabricated ~-97% "return" in the previous version of this test
(scripts/backtest_gate_legs_nifty.py, now superseded by this file).

WHAT CHANGED AND WHY (read this before trusting the numbers below):
  - Every trade's option premium is priced by REAL Black-Scholes, using
    the REAL ATM strike (nearest 50-point strike to spot, same rule live
    trading uses), the REAL time-to-expiry (actual NIFTY weekly Thursday
    expiry cycle, computed from the real candle timestamp), and a
    volatility estimate from REALIZED historical volatility (no stored
    historical IV exists, so this is the standard, defensible proxy —
    see engine/backtest/bs_pricer.py's docstring for the full accuracy
    statement: strike/expiry/spot are exact; sigma is an estimate).
  - Premium now DECAYS with time even if spot doesn't move (real theta
    decay), unlike the old constant-delta linear model which had none.
  - This does not touch your live app in any way -- it's a new, separate,
    read-only endpoint (/backtest/run_bs) and engine module (engine_bs.py).
    Your original /backtest/run and INDEX_CONFIG are completely untouched.

FULL TRADE LOG: every individual trade (entry/exit time, strike, spot,
premium, sigma, lots, P&L, exit reason) is written to a CSV per
combination, not just a summary -- so you can audit exactly what happened
on every single trade, not just trust an aggregate number.

Run from the project root, with `uvicorn api.main:app --host 127.0.0.1
--port 8001` already running and NIFTY50 candle history already backfilled
(same 2-year backfill you already ran for the previous test -- no need to
re-backfill):

    python -m scripts.backtest_gate_legs_nifty_bs
"""
import csv
import requests

BASE = "http://127.0.0.1:8001"
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


def run_one(slug: str, require_chart: bool, require_vwap: bool) -> dict:
    params = {
        "candle_limit": CANDLE_LIMIT,
        "require_chart_confirmation": require_chart,
        "require_vwap": require_vwap,
        "log_all_trades": True,
        # initial_capital, risk_per_trade_pct, hard_sl_pct, time_stop_minutes,
        # tsl_*, min_confidence intentionally OMITTED -> each falls back to
        # your CURRENT LIVE NIFTY50 config, identical across all 4 runs, so
        # gate combination is the only variable being compared.
    }
    resp = requests.get(f"{BASE}/backtest/run_bs/{INDEX_ID}", params=params, timeout=600)
    return resp.json()


def write_trade_log(slug: str, trades: list[dict]) -> str:
    filename = f"backtest_trades_{slug}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writeheader()
        for t in trades:
            writer.writerow({k: t.get(k) for k in TRADE_LOG_FIELDS})
    return filename


def main():
    print(f"Running 4 gate-combination backtests on {INDEX_ID} with REAL Black-Scholes pricing, "
          f"holding all other risk params at your current LIVE settings...\n")

    summary_rows = []
    for slug, label, req_chart, req_vwap in GATE_COMBINATIONS:
        print(f"[{label}] require_chart_confirmation={req_chart}, require_vwap={req_vwap} ... ", end="", flush=True)
        payload = run_one(slug, req_chart, req_vwap)

        if payload.get("status") != "ok":
            print(f"ERROR: {payload.get('message', 'unknown error')}")
            summary_rows.append({"combination": label, "error": payload.get("message")})
            continue

        summary = payload.get("summary") or {}
        diagnostics = payload.get("diagnostics") or {}
        trades = payload.get("trades") or []
        csv_file = write_trade_log(slug, trades)

        print(f"OK — {summary.get('trade_count', 0)} trades, log written to {csv_file}")

        summary_rows.append({
            "combination": label,
            "candle_count": payload.get("candle_count"),
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

    print("\nIMPORTANT: if 'zero-volume candles' above is high for a combination that requires VWAP "
          "(Quant+VWAP or All together), that leg was mostly SKIPPED across the tested period (no real "
          "volume data to check against), not genuinely enforced -- the confluence_gate/gate.py module "
          "itself documents that 2021-2024 NIFTY historical data has zero recorded volume in this app's "
          "data source, a known Fyers data-provider limitation. In that case, 'Quant+VWAP' and 'All "
          "together' rows may closely resemble their non-VWAP counterparts simply because VWAP had "
          "nothing to check, not because VWAP was tested and found neutral.")

    print("\nAccuracy note: strike, expiry, and spot price in every trade above are REAL (not "
          "approximated). Volatility (sigma) is estimated from realized historical volatility of the "
          "same candles, since no historical implied-volatility data is stored anywhere in this app -- "
          "this is standard practice for backtesting without stored option-chain history, but it is a "
          "proxy, not the exact historical IV market makers were quoting on those dates.")


if __name__ == "__main__":
    main()
