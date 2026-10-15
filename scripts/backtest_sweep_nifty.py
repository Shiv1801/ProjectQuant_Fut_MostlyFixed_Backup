"""
NIFTY50-only backtest parameter sweep, adapted from backtest_sweep.py
for the current app (BankNifty/FinNifty removed, real paper capital
now Rs 30,000 per Shivam's live settings as of 2026-09-28).

REAL, STATED LIMITATION (do not skip past this): the backtest engine
(engine/backtest/engine.py) does NOT support toggling
require_chart_confirmation or require_vwap -- chart confirmation is
hardcoded ON inside run_backtest()'s call to evaluate_confluence(), and
VWAP isn't wired into the backtest at all (no historical VWAP model
call exists there). OFI, PCR, Max Pain, and OI Change also can't be
backtested (no historical option-chain/depth snapshots are retained),
so quant confidence here only reflects Hurst regime + Z-score, not the
full live 5-model vote. This sweep therefore tunes SL/TSL/risk%/
min_confidence ONLY, against a fixed chart-confirmation-mandatory,
no-VWAP baseline -- it does NOT validate your live require_chart_confirmation/
require_vwap toggle choices one way or the other. Treat "best settings"
below as best FOR THAT SUBSET of the real gate, not the full live
system.

Run from the project root, with `uvicorn api.main:app --host 127.0.0.1
--port 8001` already running in another terminal:
    python -m scripts.backtest_sweep_nifty
"""
import requests
import csv
import itertools

BASE = "http://127.0.0.1:8001"
INDEX_ID = "NIFTY50"  # only index left in the app

# Grid: same shape as the original sweep, extended slightly to bracket
# your current live values (Hard SL / TSL activation / min confidence)
# so the sweep can tell you whether to move up, down, or stay.
PARAM_GRID = {
    "hard_sl_pct": [5, 7, 9, 12, 15],
    "tsl_activation_pct": [3, 5, 7, 10],
    "min_confidence": [0.5, 0.7, 0.85, 0.9, 0.95],
}
# Fixed (not swept). initial_capital matches your real current paper
# capital (Rs 30,000) so return %/drawdown are realistic for YOUR
# account size, not a generic Rs 100,000 default.
FIXED_PARAMS = {
    "initial_capital": 30000,
    "time_stop_minutes": 7,       # matches your current live default
    "tsl_base_trail_pct": 3,      # matches your current live default
    "risk_per_trade_pct": 3,      # your last manually-set value (3%)
}

OUTPUT_FILE = "backtest_sweep_nifty_results.csv"


def run_one_backtest(params: dict, walk_forward: bool) -> dict:
    query = dict(params)
    query["walk_forward"] = walk_forward
    query["save_to_history"] = True
    resp = requests.get(f"{BASE}/backtest/run/{INDEX_ID}", params=query, timeout=60)
    return resp.json()


def compute_composite_score(row: dict, min_trades: int = 5) -> float | None:
    """Same scoring as the original sweep: 45% return, 30% win rate,
    25% (inverse) drawdown, excluded if fewer than min_trades test trades."""
    trades = row.get("test_trades")
    win_rate = row.get("test_win_rate")
    return_pct = row.get("test_return_pct")
    max_dd = row.get("test_max_dd_pct")

    if trades is None or trades < min_trades:
        return None
    if win_rate is None or return_pct is None or max_dd is None:
        return None

    win_rate_component = win_rate
    return_component = max(-1.0, min(1.0, return_pct / 8.0))
    drawdown_component = max(0.0, 1.0 - (max_dd / 30.0))

    score = (0.45 * return_component) + (0.30 * win_rate_component) + (0.25 * drawdown_component)
    return round(score, 4)


def flatten_result(params, walk_forward, payload):
    row = {
        "walk_forward": walk_forward,
        "hard_sl_pct": params["hard_sl_pct"], "tsl_activation_pct": params["tsl_activation_pct"],
        "min_confidence": params["min_confidence"],
    }
    if payload.get("status") != "ok":
        row["error"] = payload.get("message", "unknown error")
        return row

    if walk_forward:
        test_summary = payload.get("test", {}).get("summary") or {}
        train_summary = payload.get("train", {}).get("summary") or {}
        row.update({
            "test_trades": test_summary.get("trade_count"), "test_win_rate": test_summary.get("win_rate"),
            "test_return_pct": test_summary.get("return_pct"), "test_max_dd_pct": test_summary.get("max_drawdown_pct"),
            "test_avg_win_pct": test_summary.get("avg_win_pct"), "test_avg_loss_pct": test_summary.get("avg_loss_pct"),
            "train_return_pct": train_summary.get("return_pct"), "train_trades": train_summary.get("trade_count"),
        })
        row["composite_score"] = compute_composite_score(row)
    else:
        summary = payload.get("summary") or {}
        row.update({
            "full_trades": summary.get("trade_count"), "full_win_rate": summary.get("win_rate"),
            "full_return_pct": summary.get("return_pct"), "full_max_dd_pct": summary.get("max_drawdown_pct"),
            "full_avg_win_pct": summary.get("avg_win_pct"), "full_avg_loss_pct": summary.get("avg_loss_pct"),
        })
    return row


def main():
    combos = list(itertools.product(
        PARAM_GRID["hard_sl_pct"], PARAM_GRID["tsl_activation_pct"], PARAM_GRID["min_confidence"],
    ))
    total_runs = len(combos) * 2  # x2 for walk_forward True/False
    print(f"Running {len(combos)} parameter combinations x 2 modes = {total_runs} backtests on {INDEX_ID}...")
    print(f"Fixed: {FIXED_PARAMS}")
    print("REMINDER: this sweep cannot test require_chart_confirmation/require_vwap -- "
          "chart confirmation is mandatory and VWAP is unused inside the backtest engine.\n")

    results = []
    run_num = 0

    for hard_sl, tsl_act, min_conf in combos:
        params = dict(FIXED_PARAMS)
        params["hard_sl_pct"] = hard_sl
        params["tsl_activation_pct"] = tsl_act
        params["min_confidence"] = min_conf

        for walk_forward in (True, False):
            run_num += 1
            print(f"[{run_num}/{total_runs}] SL={hard_sl} TSL={tsl_act} conf={min_conf} wf={walk_forward}...", end=" ")
            try:
                payload = run_one_backtest(params, walk_forward)
                row = flatten_result(params, walk_forward, payload)
                results.append(row)
                print("OK" if "error" not in row else f"ERROR: {row['error']}")
            except requests.exceptions.RequestException as e:
                print(f"FAILED: {e}")
                results.append({"walk_forward": walk_forward, **params, "error": str(e)})

    fieldnames = sorted({k for row in results for k in row.keys()})
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. Wrote {len(results)} rows to {OUTPUT_FILE}")

    scored_rows = [r for r in results if r.get("composite_score") is not None]
    scored_rows.sort(key=lambda r: r["composite_score"], reverse=True)

    print(f"\nTop 10 by COMPOSITE score (blends win rate + return % + drawdown, min 5 test trades to qualify):")
    print(f"{'SL':<5} {'TSL':<5} {'Conf':<6} {'Score':<7} {'Return%':<9} {'WinRate':<9} {'MaxDD%':<8} {'Trades'}")
    for r in scored_rows[:10]:
        print(f"{r['hard_sl_pct']:<5} {r['tsl_activation_pct']:<5} {r['min_confidence']:<6} "
              f"{r['composite_score']:<7} {r['test_return_pct']:<9} {r['test_win_rate']:<9} {r['test_max_dd_pct']:<8} {r['test_trades']}")

    if not scored_rows:
        print("\nNo combinations had at least 5 test trades in the TEST split — either not enough candle "
              "history is backfilled yet, or min_confidence values swept are all too strict for current data. "
              "Run /backfill/NIFTY50 (or /backfill/extended/NIFTY50) first if candle_count is low.")
    else:
        best = scored_rows[0]
        print(f"\nBEST OVERALL for NIFTY50 (current: SL={FIXED_PARAMS.get('hard_sl_pct', 'live default')}, "
              f"risk/trade={FIXED_PARAMS['risk_per_trade_pct']}%):")
        print(f"  hard_sl_pct={best['hard_sl_pct']}, tsl_activation_pct={best['tsl_activation_pct']}, "
              f"min_confidence={best['min_confidence']}")
        print(f"  -> test_return={best['test_return_pct']}%, win_rate={best['test_win_rate']}, "
              f"max_dd={best['test_max_dd_pct']}%, trades={best['test_trades']}")
        print(f"\nTo apply: open the Positions & Risk tab, set these three values under NIFTY 50, and Save. "
              f"(Or POST /backtest/apply with the run_id from {OUTPUT_FILE}/backtest history.)")


if __name__ == "__main__":
    main()
