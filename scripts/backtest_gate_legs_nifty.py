"""
Independent backtest comparing the 4 confluence-gate combinations on
NIFTY50, over ~2 years of 1-min candle history:

  1) Quant only          (require_chart_confirmation=False, require_vwap=False)
  2) Quant + Chart        (require_chart_confirmation=True,  require_vwap=False)
  3) Quant + VWAP         (require_chart_confirmation=False, require_vwap=True)
  4) All together (Quant+Chart+VWAP)
                          (require_chart_confirmation=True,  require_vwap=True)

This does NOT touch your live app settings. /backtest/run/{index_id}
only writes to the backtest_runs HISTORY table (via save_to_history) --
it never writes to INDEX_CONFIG, which is what live/paper trading
actually reads. Your running bot's settings are completely unaffected
by anything this script does.

Every other risk param (Hard SL, Time Stop, TSL settings, Risk/Trade,
Min Confidence) is held at your CURRENT LIVE values for all 4 runs, so
the only thing that changes between the 4 backtests is the gate
combination itself -- isolating the effect of Chart/VWAP cleanly.

REAL, STATED LIMITATIONS (same ones documented in engine/backtest/engine.py
and scripts/backtest_sweep_nifty.py -- repeated here, not hidden):
  - OFI, PCR, Max Pain, OI Change are NOT backtestable -- no historical
    option-chain/depth snapshots are retained, only candle closes. Quant
    confidence here reflects Hurst regime + Z-score only, not the full
    5-model live vote.
  - Option premium is approximated via a constant-delta linear model
    (delta=0.35 default), not real options pricing (no gamma/theta/IV
    surface evolution).
  - VWAP requires real volume in the candle history. If 2 years of
    candles were backfilled via a plain (non-extended) backfill call,
    volume may be sparse/zero for older chunks -- check the printed
    "candles with usable VWAP" diagnostic below. If VWAP coverage looks
    thin, re-run after /backfill/extended/NIFTY50?days=730 (see
    instructions below) so this comparison is trustworthy for legs 3/4.

Run from the project root, with `uvicorn api.main:app --host 127.0.0.1
--port 8001` already running (your token must already be logged in,
since VWAP-quality candle backfill needs it):

    python -m scripts.backtest_gate_legs_nifty

Before running for the first time, or if candle_count comes back low,
first pull 2 years of history (only needs to be done once, takes a
while -- Fyers rate-limits chunked historical requests):

    curl "http://127.0.0.1:8001/backfill/extended/NIFTY50?days=730"
"""
import requests

BASE = "http://127.0.0.1:8001"
INDEX_ID = "NIFTY50"

# ~2 years of 1-min candles. NSE trading day is ~375 minutes; roughly
# 250 trading days/year -> ~93,750 candles/year -> ~187,500 for 2 years.
# Requesting comfortably above that so the backtest uses everything the
# backfill actually delivered, capped only by what's really in the DB.
CANDLE_LIMIT = 200_000

GATE_COMBINATIONS = [
    ("Quant only",        False, False),
    ("Quant + Chart",     True,  False),
    ("Quant + VWAP",      False, True),
    ("All together (Quant+Chart+VWAP)", True, True),
]


def check_candle_coverage():
    resp = requests.get(f"{BASE}/candles/count/{INDEX_ID}", timeout=30)
    payload = resp.json()
    print(f"Candle coverage check: {payload}\n")
    return payload


def run_one(label: str, require_chart: bool, require_vwap: bool) -> dict:
    params = {
        "candle_limit": CANDLE_LIMIT,
        "require_chart_confirmation": require_chart,
        "require_vwap": require_vwap,
        "walk_forward": True,          # 70/30 train/test split, out-of-sample check
        "save_to_history": True,
        # risk_per_trade_pct, hard_sl_pct, time_stop_minutes, tsl_*,
        # min_confidence intentionally OMITTED -> each falls back to
        # your CURRENT LIVE NIFTY50 config, held identical across all
        # 4 runs so gate combination is the only variable.
    }
    resp = requests.get(f"{BASE}/backtest/run/{INDEX_ID}", params=params, timeout=180)
    payload = resp.json()
    return payload


def summarize(label: str, payload: dict) -> dict:
    if payload.get("status") != "ok":
        return {"combination": label, "error": payload.get("message", "unknown error")}

    candle_count = payload.get("candle_count")
    test = payload.get("test", {}) or {}
    train = payload.get("train", {}) or {}
    test_summary = test.get("summary") or {}
    train_summary = train.get("summary") or {}

    return {
        "combination": label,
        "candle_count": candle_count,
        "train_trades": train_summary.get("trade_count"),
        "train_return_pct": train_summary.get("return_pct"),
        "train_win_rate": train_summary.get("win_rate"),
        "test_trades": test_summary.get("trade_count"),
        "test_return_pct": test_summary.get("return_pct"),
        "test_win_rate": test_summary.get("win_rate"),
        "test_max_dd_pct": test_summary.get("max_drawdown_pct"),
        "test_avg_win_pct": test_summary.get("avg_win_pct"),
        "test_avg_loss_pct": test_summary.get("avg_loss_pct"),
        "note": payload.get("note"),
    }


def main():
    coverage = check_candle_coverage()
    total_candles = coverage.get("count") if isinstance(coverage, dict) else None
    if total_candles is not None and total_candles < 50_000:
        print(f"WARNING: only {total_candles} candles available -- this is well short of ~2 years "
              f"(~187,500 candles). Results below will reflect whatever history IS available, not "
              f"a genuine 2-year window. Run:\n"
              f"  curl \"{BASE}/backfill/extended/{INDEX_ID}?days=730\"\n"
              f"first, then re-run this script.\n")

    print(f"Running 4 gate-combination backtests on {INDEX_ID}, holding all other risk params at "
          f"your current LIVE settings, walk-forward (70% train / 30% test)...\n")

    results = []
    for label, req_chart, req_vwap in GATE_COMBINATIONS:
        print(f"[{label}] require_chart_confirmation={req_chart}, require_vwap={req_vwap} ... ", end="", flush=True)
        payload = run_one(label, req_chart, req_vwap)
        row = summarize(label, payload)
        results.append(row)
        print("OK" if "error" not in row else f"ERROR: {row['error']}")

    print("\n" + "=" * 100)
    print(f"{'Combination':<36} {'Candles':<9} {'TrainTr':<8} {'TrainRet%':<10} {'TestTr':<7} "
          f"{'TestRet%':<9} {'TestWin%':<9} {'TestMaxDD%':<11}")
    print("-" * 100)
    for r in results:
        if "error" in r:
            print(f"{r['combination']:<36} ERROR: {r['error']}")
            continue
        win_pct = f"{r['test_win_rate']*100:.1f}" if r['test_win_rate'] is not None else "—"
        print(f"{r['combination']:<36} {str(r['candle_count']):<9} {str(r['train_trades']):<8} "
              f"{str(r['train_return_pct']):<10} {str(r['test_trades']):<7} {str(r['test_return_pct']):<9} "
              f"{win_pct:<9} {str(r['test_max_dd_pct']):<11}")
    print("=" * 100)

    print("\nREMINDER: this compares gate combinations only -- Hard SL/Time Stop/TSL/Risk-per-trade/"
          "Min Confidence were held at your live config for every row above, so differences you see "
          "are attributable to the Chart/VWAP legs, not risk settings. Quant confidence in every row "
          "reflects Hurst + Z-score only (OFI/PCR unavailable historically) -- not the full live "
          "5-model vote. Treat 'test' columns (out-of-sample 30%) as the more trustworthy figures; "
          "'train' numbers can curve-fit to the data they were selected on.")


if __name__ == "__main__":
    main()
