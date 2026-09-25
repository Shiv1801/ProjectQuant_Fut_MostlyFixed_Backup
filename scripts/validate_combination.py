"""
Validates ONE specific parameter combination with a proper walk-forward
(train/test) split, using the SAME data/logic as standalone_backtest.py.
Purpose: before applying a combination found via full-history sweep to
live settings, confirm it actually holds up out-of-sample — a
full-history "best" result has no train/test separation and can look
good purely from fitting the whole dataset at once.

Usage:
    python scripts/validate_combination.py

Prompts for index, then runs the winning combination identified from
your sweep: SL=7.5, TimeStop=10, TSL Activation=5, TSL Base=3,
Risk/Trade=1, Confidence=0.95 (edit COMBINATION below to check a
different one).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from standalone_backtest import (
    get_candles, precompute_signals, precompute_chart_confirmations, run_single_combination,
)

# The combination to validate — edit these to check a different one.
COMBINATION = {
    "hard_sl_pct": 7.5,
    "time_stop_minutes": 10,
    "tsl_activation_pct": 5.0,
    "tsl_base_trail_pct": 3.0,
    "risk_per_trade_pct": 1,
    "min_confidence": 0.95,
}


def main():
    print("=" * 70)
    print("SINGLE-COMBINATION WALK-FORWARD VALIDATOR")
    print("=" * 70)
    print(f"\nValidating: {COMBINATION}\n")

    index_id = input("Index (NIFTY50 / BANKNIFTY / FINNIFTY): ").strip().upper()
    if index_id not in ("NIFTY50", "BANKNIFTY", "FINNIFTY"):
        print(f"Invalid index '{index_id}'")
        sys.exit(1)

    refresh = input("Force fresh Fyers data fetch even if cache exists? (y/n): ").strip().lower() == "y"
    candles = get_candles(index_id, force_refresh=refresh)

    if len(candles) < 150:
        print(f"ERROR: only {len(candles)} candles available, need at least 150.")
        sys.exit(1)

    split = int(len(candles) * 0.7)
    train_candles = candles[:split]
    test_candles = candles[split:]

    print(f"\nTotal candles: {len(candles)} -> Train: {len(train_candles)}, Test: {len(test_candles)}\n")

    results = {}
    for seg_name, seg_candles in [("TRAIN", train_candles), ("TEST", test_candles)]:
        print(f"Running {seg_name} segment ({len(seg_candles)} candles)...")
        import numpy as np
        closes = np.array([c["close"] for c in seg_candles])
        directions, confidences = precompute_signals(closes)
        confirmed = precompute_chart_confirmations(seg_candles, directions)

        result = run_single_combination(
            seg_candles, directions, confidences, confirmed, index_id, **COMBINATION,
        )
        results[seg_name] = result

    print("\n" + "=" * 70)
    print(f"RESULTS for {index_id} — SL={COMBINATION['hard_sl_pct']}%, "
          f"TimeStop={COMBINATION['time_stop_minutes']}min, "
          f"TSL Act={COMBINATION['tsl_activation_pct']}%, "
          f"TSL Base={COMBINATION['tsl_base_trail_pct']}%, "
          f"Risk={COMBINATION['risk_per_trade_pct']}%, "
          f"Confidence={COMBINATION['min_confidence']}")
    print("=" * 70)

    for seg_name in ("TRAIN", "TEST"):
        r = results[seg_name]
        print(f"\n{seg_name}:")
        if r["trade_count"] == 0:
            print(f"  No trades. {r.get('note', '')}")
            continue
        print(f"  Trades: {r['trade_count']}")
        print(f"  Win Rate: {r['win_rate']*100:.1f}%")
        print(f"  Return: {r['return_pct']}%")
        print(f"  Max Drawdown: {r['max_drawdown_pct']}%")
        print(f"  Avg Win: {r['avg_win_pct']}%  |  Avg Loss: {r['avg_loss_pct']}%")
        print(f"  Final Capital: {r['final_capital']:,.2f}")

    # Simple verdict
    print("\n" + "-" * 70)
    train_r = results["TRAIN"]
    test_r = results["TEST"]
    if test_r["trade_count"] == 0:
        print("VERDICT: Cannot validate — no trades in the TEST segment.")
    elif test_r["return_pct"] is not None and test_r["return_pct"] > 0:
        gap = (train_r["return_pct"] or 0) - test_r["return_pct"]
        if abs(gap) < (train_r["return_pct"] or 1) * 0.5:
            print(f"VERDICT: TEST return ({test_r['return_pct']}%) is positive and reasonably "
                  f"consistent with TRAIN ({train_r['return_pct']}%) — this combination looks genuinely robust, "
                  f"not just fitted to the training window. Worth considering for live settings.")
        else:
            print(f"VERDICT: TEST return ({test_r['return_pct']}%) is positive but drops off a lot "
                  f"from TRAIN ({train_r['return_pct']}%) — some overfitting signal. Still positive, "
                  f"but treat with more caution.")
    else:
        print(f"VERDICT: TEST return is negative or zero ({test_r['return_pct']}%) despite a good-looking "
              f"TRAIN result ({train_r['return_pct']}%) — this is the overfitting pattern. "
              f"Do NOT apply this combination to live settings.")


if __name__ == "__main__":
    main()
