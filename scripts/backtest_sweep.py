"""
Automated backtest parameter sweep. Runs a grid of parameter
combinations against the LOCAL running server's /backtest/run endpoint
(needs `uvicorn api.main:app` already running), across all 3 indices,
both walk-forward and full-history modes, and writes a ranked CSV.

Run from the project root:
    python -m scripts.backtest_sweep

Requires: requests (already in your venv from the Fyers SDK dependency)
"""
import requests
import csv
import itertools
from datetime import datetime

BASE = "http://127.0.0.1:8001"
INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY"]

# Grid informed by the first 4 combinations already tested: SL and
# Confidence each showed real signal (index-dependent); TSL activation
# was weaker; this grid focuses there plus the interaction combos
# identified as worth checking next, rather than pure brute force.
PARAM_GRID = {
    "hard_sl_pct": [20, 22, 27],
    "tsl_activation_pct": [10, 12, 15],
    "min_confidence": [0.5, 0.6, 0.65],
}
# Fixed (not swept, to keep the grid manageable — change here if you want these varied too)
FIXED_PARAMS = {
    "time_stop_minutes": 18,
    "tsl_base_trail_pct": 12,
    "risk_per_trade_pct": 2,
}

OUTPUT_FILE = "backtest_sweep_results.csv"


def run_one_backtest(index_id: str, params: dict, walk_forward: bool) -> dict:
    query = dict(params)
    query["walk_forward"] = walk_forward
    query["save_to_history"] = True
    resp = requests.get(f"{BASE}/backtest/run/{index_id}", params=query, timeout=60)
    return resp.json()


def compute_composite_score(row: dict, min_trades: int = 5) -> float | None:
    """
    Combines win rate, return %, and (inverse) drawdown into one
    comparable score, so 'best' reflects all three stated goals instead
    of ranking by return alone. Weights are deliberately return-leaning
    since profit is the ultimate point, but win rate and drawdown both
    still meaningfully move the score.

    Returns None (excluded from ranking) if the sample size is too
    small to trust — a 2-3 trade test window can show a flattering win
    rate or drawdown purely by luck, which is exactly the trap the
    walk-forward split exists to catch, not reintroduce via a new metric.
    """
    trades = row.get("test_trades")
    win_rate = row.get("test_win_rate")
    return_pct = row.get("test_return_pct")
    max_dd = row.get("test_max_dd_pct")

    if trades is None or trades < min_trades:
        return None
    if win_rate is None or return_pct is None or max_dd is None:
        return None

    # Normalize each component to a roughly 0-1ish comparable range.
    # Return scale calibrated to the ACTUAL range seen across real
    # sweep results (roughly -20% to +5%, not a symmetric +-20%) —
    # a naive symmetric scale under-credits realistic positive results
    # like a modest +0.5-2% test return, which given every baseline
    # result so far has been negative, IS the meaningful win.
    win_rate_component = win_rate  # already 0-1
    return_component = max(-1.0, min(1.0, return_pct / 8.0))  # +8% maps to full credit, more realistic ceiling
    drawdown_component = max(0.0, 1.0 - (max_dd / 30.0))  # 0% DD -> 1.0, 30%+ DD -> 0.0

    # Weights: profit matters most, but win rate and drawdown both count meaningfully.
    score = (0.45 * return_component) + (0.30 * win_rate_component) + (0.25 * drawdown_component)
    return round(score, 4)


def flatten_result(index_id, params, walk_forward, payload):
    row = {
        "index_id": index_id, "walk_forward": walk_forward,
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
    total_runs = len(combos) * len(INDICES) * 2  # x2 for walk_forward True/False
    print(f"Running {len(combos)} parameter combinations x {len(INDICES)} indices x 2 modes = {total_runs} backtests...")
    print("This will take a few minutes. Progress will print below.\n")

    results = []
    run_num = 0

    for hard_sl, tsl_act, min_conf in combos:
        params = dict(FIXED_PARAMS)
        params["hard_sl_pct"] = hard_sl
        params["tsl_activation_pct"] = tsl_act
        params["min_confidence"] = min_conf

        for index_id in INDICES:
            for walk_forward in (True, False):
                run_num += 1
                print(f"[{run_num}/{total_runs}] {index_id} SL={hard_sl} TSL={tsl_act} conf={min_conf} wf={walk_forward}...", end=" ")
                try:
                    payload = run_one_backtest(index_id, params, walk_forward)
                    row = flatten_result(index_id, params, walk_forward, payload)
                    results.append(row)
                    print("OK" if "error" not in row else f"ERROR: {row['error']}")
                except requests.exceptions.RequestException as e:
                    print(f"FAILED: {e}")
                    results.append({"index_id": index_id, "walk_forward": walk_forward, **params, "error": str(e)})

    fieldnames = sorted({k for row in results for k in row.keys()})
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. Wrote {len(results)} rows to {OUTPUT_FILE}")

    # Rank by composite score (blends win rate + return + drawdown, per
    # your stated goal of all three together, not return alone) — only
    # among walk-forward TEST rows with enough trades to trust (min 5).
    scored_rows = [r for r in results if r.get("composite_score") is not None]
    scored_rows.sort(key=lambda r: r["composite_score"], reverse=True)

    print(f"\nTop 10 by COMPOSITE score (blends win rate + return % + drawdown, min 5 test trades to qualify):")
    print(f"{'Index':<10} {'SL':<5} {'TSL':<5} {'Conf':<6} {'Score':<7} {'Return%':<9} {'WinRate':<9} {'MaxDD%':<8} {'Trades'}")
    for r in scored_rows[:10]:
        print(f"{r['index_id']:<10} {r['hard_sl_pct']:<5} {r['tsl_activation_pct']:<5} {r['min_confidence']:<6} "
              f"{r['composite_score']:<7} {r['test_return_pct']:<9} {r['test_win_rate']:<9} {r['test_max_dd_pct']:<8} {r['test_trades']}")

    if not scored_rows:
        print("\nNo combinations had at least 5 test trades — results are too thin to rank reliably. "
              "Consider a longer candle history or a lower min_trades threshold in compute_composite_score().")

    # Also show per-index best, since a single 'best overall' can hide
    # that different indices may want different settings.
    print("\nBest combination PER INDEX (by composite score):")
    for index_id in INDICES:
        index_rows = [r for r in scored_rows if r["index_id"] == index_id]
        if index_rows:
            best = index_rows[0]
            print(f"  {index_id}: SL={best['hard_sl_pct']} TSL={best['tsl_activation_pct']} conf={best['min_confidence']} "
                  f"-> score={best['composite_score']} (return={best['test_return_pct']}%, win_rate={best['test_win_rate']}, dd={best['test_max_dd_pct']}%)")
        else:
            print(f"  {index_id}: no qualifying combination (insufficient trade count in all runs)")


if __name__ == "__main__":
    main()
