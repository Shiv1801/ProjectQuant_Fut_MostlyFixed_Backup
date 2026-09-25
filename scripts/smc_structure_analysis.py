"""
SMC (Smart Money Concepts) Structure Alignment Analysis — tests the
PROGRAMMABLE components of SMC (market structure via swing highs/lows,
Break of Structure, Change of Character) against your real trade log,
the same rigorous way the trend-alignment test worked: pure analysis
first, no wiring into the live system until the data actually
supports it.

Deliberately excludes the more subjective SMC concepts (order blocks,
Fair Value Gaps as "institutional footprints") per the earlier,
already-agreed distinction — those don't have a rigorous, unambiguous
definition to test against real data the way swing structure does.

Definitions used (standard, not ad hoc):
  - Swing high: a candle whose high is the highest among SWING_WINDOW
    candles on either side.
  - Swing low: symmetric, lowest low among SWING_WINDOW candles on
    either side.
  - Break of Structure (BOS): price closes beyond the most recent
    confirmed swing high (bullish BOS) or swing low (bearish BOS) —
    signals trend CONTINUATION in that direction.
  - Change of Character (CHoCH): the first break in the OPPOSITE
    direction of the currently prevailing structure — signals a
    potential trend REVERSAL.

For each real trade, this finds the most recent structural signal
(BOS or CHoCH) before entry and checks whether the trade's direction
agrees or disagrees with what that signal implies, then compares win
rate and P&L between the two groups — same methodology as
trend_alignment_analysis.py, for direct comparability.

Usage:
    python scripts/smc_structure_analysis.py <path_to_trade_log.csv>

Requires the same cached candles as trend_alignment_analysis.py.
"""
import sys
import csv
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("nifty_mastery_cache/nifty_3yr_candles.json")
SWING_WINDOW = 5  # candles on each side to confirm a swing high/low


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def parse_trade_datetime(date_str: str, time_str: str) -> datetime:
    combined = f"{date_str} {time_str}"
    dt = datetime.strptime(combined, "%d %b %y %I:%M %p")
    return dt.replace(tzinfo=IST)


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run nifty_mastery_test.py first.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        candles = json.load(f)
    log(f"Loaded {len(candles)} cached candles.")
    return candles


def identify_swing_points(candles: list[dict]) -> list[dict]:
    """
    Walks the full candle series once and tags each candle as a
    confirmed swing high, swing low, or neither. A swing point can only
    be confirmed SWING_WINDOW candles after it occurs (since you need
    to see the following candles to know it was a local extreme) — this
    is handled naturally by the loop bounds, avoiding lookahead bias.
    """
    n = len(candles)
    swings = []  # list of {"index":, "epoch":, "price":, "type": "high"|"low"}

    for i in range(SWING_WINDOW, n - SWING_WINDOW):
        window_highs = [candles[j]["high"] for j in range(i - SWING_WINDOW, i + SWING_WINDOW + 1)]
        window_lows = [candles[j]["low"] for j in range(i - SWING_WINDOW, i + SWING_WINDOW + 1)]

        if candles[i]["high"] == max(window_highs):
            swings.append({"index": i, "epoch": candles[i]["epoch"], "price": candles[i]["high"], "type": "high"})
        elif candles[i]["low"] == min(window_lows):
            swings.append({"index": i, "epoch": candles[i]["epoch"], "price": candles[i]["low"], "type": "low"})

    return swings


def precompute_structure_signal_per_candle(candles: list[dict], swings: list[dict]) -> list[str | None]:
    """
    Single forward pass over the ENTIRE dataset, computing the
    prevailing BOS/CHoCH signal as of each candle index. Far more
    efficient than recomputing from scratch per-trade (which would be
    O(n) per trade, O(n*trades) overall) — this is a single O(n) pass
    that every trade then does an O(1) lookup against.
    """
    n = len(candles)
    signal_at_index: list[str | None] = [None] * n

    swing_ptr = 0
    current_high = None
    current_low = None
    prevailing_direction = None
    last_signal = None

    for i in range(n):
        while swing_ptr < len(swings) and swings[swing_ptr]["index"] <= i:
            s = swings[swing_ptr]
            if s["type"] == "high":
                current_high = s["price"]
            else:
                current_low = s["price"]
            swing_ptr += 1

        close = candles[i]["close"]
        if current_high is not None and close > current_high:
            last_signal = "CHoCH_bullish" if prevailing_direction == "bearish" else "BOS_bullish"
            prevailing_direction = "bullish"
            current_high = None
        elif current_low is not None and close < current_low:
            last_signal = "CHoCH_bearish" if prevailing_direction == "bullish" else "BOS_bearish"
            prevailing_direction = "bearish"
            current_low = None

        signal_at_index[i] = None if last_signal is None else ("bullish" if "bullish" in last_signal else "bearish")

    return signal_at_index


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/smc_structure_analysis.py <path_to_trade_log.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        log(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    log(f"Loaded {len(trades)} trades from {csv_path}")

    candles = load_candles()
    epoch_to_index = {c["epoch"]: i for i, c in enumerate(candles)}
    sorted_epochs = sorted(epoch_to_index.keys())

    log("Identifying all confirmed swing highs/lows across the full dataset (one pass)...")
    swings = identify_swing_points(candles)
    log(f"Found {len(swings)} confirmed swing points.")

    log("Precomputing the prevailing structure signal at every candle (single pass, reused for all trades)...")
    signal_at_index = precompute_structure_signal_per_candle(candles, swings)

    import bisect

    def find_candle_index(target_epoch: int) -> int | None:
        idx = bisect.bisect_right(sorted_epochs, target_epoch)
        if idx == 0:
            return None
        return epoch_to_index[sorted_epochs[idx - 1]]

    with_structure = []
    against_structure = []
    no_signal = 0

    log("Classifying each trade against the precomputed structure signal...")
    for t_i, t in enumerate(trades):
        entry_dt = parse_trade_datetime(t["Entry Date"], t["Entry Time"])
        entry_epoch = int(entry_dt.timestamp())
        entry_index = find_candle_index(entry_epoch)

        if entry_index is None or entry_index < SWING_WINDOW * 2:
            no_signal += 1
            continue

        signal = signal_at_index[entry_index]
        if signal is None:
            no_signal += 1
            continue

        trade_direction = "bullish" if t["Direction"] == "BULLISH" else "bearish"
        pnl = float(t["P&L"])

        if trade_direction == signal:
            with_structure.append(pnl)
        else:
            against_structure.append(pnl)

    log(f"\nTrades with no structural signal available (skipped): {no_signal}")
    log(f"Trades WITH the prevailing structure (BOS/CHoCH agrees): {len(with_structure)}")
    log(f"Trades AGAINST the prevailing structure: {len(against_structure)}")

    print("\n" + "=" * 70)
    print(f"SMC STRUCTURE ALIGNMENT ANALYSIS (swing window: {SWING_WINDOW} candles)")
    print("=" * 70)

    for label, group in [("WITH prevailing structure (BOS/CHoCH)", with_structure), ("AGAINST prevailing structure", against_structure)]:
        if not group:
            print(f"\n{label}: no trades in this group.")
            continue
        wins = [p for p in group if p > 0]
        total_pnl = sum(group)
        win_rate = len(wins) / len(group) * 100
        avg_pnl = total_pnl / len(group)
        print(f"\n{label}:")
        print(f"  Trades: {len(group)}")
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Total P&L: {total_pnl:,.2f}")
        print(f"  Avg P&L per trade: {avg_pnl:,.2f}")

    if with_structure and against_structure:
        wr_with = len([p for p in with_structure if p > 0]) / len(with_structure) * 100
        wr_against = len([p for p in against_structure if p > 0]) / len(against_structure) * 100
        avg_with = sum(with_structure) / len(with_structure)
        avg_against = sum(against_structure) / len(against_structure)

        print("\n" + "-" * 70)
        print("VERDICT:")
        if wr_with > wr_against + 5 and avg_with > avg_against:
            print(f"  Trades WITH prevailing structure meaningfully outperform "
                  f"(win rate +{wr_with-wr_against:.1f}pp, avg P&L {avg_with:.0f} vs {avg_against:.0f}).")
            print("  This SUPPORTS adding a BOS/CHoCH structure filter to the live gate.")
        elif wr_against > wr_with + 5 and avg_against > avg_with:
            print(f"  Trades AGAINST prevailing structure perform better (win rate +{wr_against-wr_with:.1f}pp).")
            print("  This DOES NOT support a simple 'trade with structure' filter as designed.")
        else:
            print("  No large, clear difference between the two groups in this data.")
            print("  A BOS/CHoCH structure filter, as tested here, may not meaningfully change results.")


if __name__ == "__main__":
    main()
