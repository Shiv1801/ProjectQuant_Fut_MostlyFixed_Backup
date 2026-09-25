"""
Cross-Reference Check — tests whether the "with structure" group's
outsized average P&L (found in smc_structure_analysis.py) is actually
just the same low-premium/high-leverage trades we already identified
as an untrustworthy concentration (57% of total 3-year P&L came from
just 6.4% of trades, all entering at very low premium), or a genuinely
distinct, new finding.

Reuses the exact same swing/BOS/CHoCH logic as smc_structure_analysis.py
(copied here for a fully standalone script) so the classification is
identical and directly comparable.

Usage:
    python scripts/cross_reference_check.py <path_to_trade_log.csv>
"""
import sys
import csv
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import bisect

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("nifty_mastery_cache/nifty_3yr_candles.json")
SWING_WINDOW = 5
LOW_PREMIUM_THRESHOLD = 10.0  # matches the earlier "< Rs 10" bucket that showed 57% P&L concentration


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def parse_trade_datetime(date_str: str, time_str: str) -> datetime:
    combined = f"{date_str} {time_str}"
    dt = datetime.strptime(combined, "%d %b %y %I:%M %p")
    return dt.replace(tzinfo=IST)


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        return json.load(f)


def identify_swing_points(candles: list[dict]) -> list[dict]:
    n = len(candles)
    swings = []
    for i in range(SWING_WINDOW, n - SWING_WINDOW):
        window_highs = [candles[j]["high"] for j in range(i - SWING_WINDOW, i + SWING_WINDOW + 1)]
        window_lows = [candles[j]["low"] for j in range(i - SWING_WINDOW, i + SWING_WINDOW + 1)]
        if candles[i]["high"] == max(window_highs):
            swings.append({"index": i, "epoch": candles[i]["epoch"], "price": candles[i]["high"], "type": "high"})
        elif candles[i]["low"] == min(window_lows):
            swings.append({"index": i, "epoch": candles[i]["epoch"], "price": candles[i]["low"], "type": "low"})
    return swings


def precompute_structure_signal_per_candle(candles: list[dict], swings: list[dict]) -> list[str | None]:
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
        print("Usage: python scripts/cross_reference_check.py <path_to_trade_log.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    with open(csv_path, encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    log(f"Loaded {len(trades)} trades from {csv_path}")

    candles = load_candles()
    log(f"Loaded {len(candles)} cached candles.")
    epoch_to_index = {c["epoch"]: i for i, c in enumerate(candles)}
    sorted_epochs = sorted(epoch_to_index.keys())

    swings = identify_swing_points(candles)
    signal_at_index = precompute_structure_signal_per_candle(candles, swings)

    def find_candle_index(target_epoch: int):
        idx = bisect.bisect_right(sorted_epochs, target_epoch)
        if idx == 0:
            return None
        return epoch_to_index[sorted_epochs[idx - 1]]

    # Classify every trade on BOTH dimensions at once: structure alignment AND low-premium status
    with_structure_low_premium = 0
    with_structure_normal_premium = 0
    against_structure_low_premium = 0
    against_structure_normal_premium = 0

    with_structure_low_premium_pnl = 0.0
    with_structure_normal_premium_pnl = 0.0

    for t in trades:
        entry_dt = parse_trade_datetime(t["Entry Date"], t["Entry Time"])
        entry_epoch = int(entry_dt.timestamp())
        entry_index = find_candle_index(entry_epoch)
        if entry_index is None or entry_index < SWING_WINDOW * 2:
            continue

        signal = signal_at_index[entry_index]
        if signal is None:
            continue

        trade_direction = "bullish" if t["Direction"] == "BULLISH" else "bearish"
        is_low_premium = float(t["Entry ₹"]) < LOW_PREMIUM_THRESHOLD
        pnl = float(t["P&L"])
        aligned = (trade_direction == signal)

        if aligned:
            if is_low_premium:
                with_structure_low_premium += 1
                with_structure_low_premium_pnl += pnl
            else:
                with_structure_normal_premium += 1
                with_structure_normal_premium_pnl += pnl
        else:
            if is_low_premium:
                against_structure_low_premium += 1
            else:
                against_structure_normal_premium += 1

    total_with_structure = with_structure_low_premium + with_structure_normal_premium

    print("\n" + "=" * 70)
    print("CROSS-REFERENCE: does 'with structure' overlap with low-premium/high-leverage trades?")
    print("=" * 70)

    print(f"\nOf {total_with_structure} trades classified 'WITH structure':")
    print(f"  Low-premium (< Rs{LOW_PREMIUM_THRESHOLD}): {with_structure_low_premium} "
          f"({with_structure_low_premium/total_with_structure*100:.1f}%), "
          f"contributing P&L: {with_structure_low_premium_pnl:,.2f}")
    print(f"  Normal-premium: {with_structure_normal_premium} "
          f"({with_structure_normal_premium/total_with_structure*100:.1f}%), "
          f"contributing P&L: {with_structure_normal_premium_pnl:,.2f}")

    total_with_structure_pnl = with_structure_low_premium_pnl + with_structure_normal_premium_pnl
    if total_with_structure_pnl != 0:
        low_premium_share = with_structure_low_premium_pnl / total_with_structure_pnl * 100
        print(f"\nShare of 'WITH structure' group's TOTAL P&L coming from low-premium trades: {low_premium_share:.1f}%")

    print("\n" + "-" * 70)
    print("VERDICT:")
    overall_low_premium_share_of_all_trades = 6.4  # from the earlier established finding
    if total_with_structure_pnl != 0 and low_premium_share > overall_low_premium_share_of_all_trades * 2:
        print(f"  Low-premium trades are heavily OVER-represented in the 'with structure' group's P&L")
        print(f"  ({low_premium_share:.1f}% vs the overall dataset's {overall_low_premium_share_of_all_trades}% baseline).")
        print("  This SUGGESTS the earlier 'structure alignment -> bigger wins' finding is largely")
        print("  the SAME low-premium/high-leverage concentration showing up again, not a distinct signal.")
    else:
        print(f"  Low-premium trades are NOT heavily over-represented in the 'with structure' group")
        print(f"  ({low_premium_share:.1f}% vs the {overall_low_premium_share_of_all_trades}% baseline).")
        print("  This SUPPORTS structure alignment being a genuinely distinct finding, not just the")
        print("  same low-premium concentration issue reappearing.")


if __name__ == "__main__":
    main()
