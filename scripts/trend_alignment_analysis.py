"""
Trend-Alignment Analysis — directly tests your real observation: "the
system flips buy/sell on minor fluctuations while the market moves
mostly one way." For every real trade in a mastery-test trade log CSV,
this looks back at the broader trend (configurable window, default 3
hours) using the SAME cached candle data already fetched, and splits
trades into "with the broader trend" vs "against it" — then compares
win rate and P&L between the two groups.

If trades against the broader trend perform meaningfully worse, that's
direct, real evidence (not speculation) that a higher-timeframe trend
filter would help — and tells us how much, before we build anything.

Usage:
    python scripts/trend_alignment_analysis.py <path_to_trade_log.csv>

Requires the cached candles used by nifty_mastery_test.py to still be
present at nifty_mastery_cache/nifty_3yr_candles.json (does not
re-fetch from Fyers — this is pure analysis on data you already have).
"""
import sys
import csv
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("nifty_mastery_cache/nifty_3yr_candles.json")
TREND_LOOKBACK_MINUTES = 180  # 3 hours -- configurable, this is the "broader trend" window


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def parse_trade_datetime(date_str: str, time_str: str) -> datetime:
    """Parses the exact 'Entry Date'/'Entry Time' format used by the
    mastery test's trade log (e.g. '11 Sep 23', '11:40 AM')."""
    combined = f"{date_str} {time_str}"
    dt = datetime.strptime(combined, "%d %b %y %I:%M %p")
    return dt.replace(tzinfo=IST)


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run nifty_mastery_test.py first "
            f"(even just to populate the cache) so this script has candle data to work with.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        candles = json.load(f)
    log(f"Loaded {len(candles)} cached candles.")
    return candles


def build_epoch_index(candles: list[dict]) -> dict:
    """Maps epoch -> close price for fast lookup, since we need to find
    the price ~3 hours before each trade's entry, not just adjacent candles."""
    return {c["epoch"]: c["close"] for c in candles}


def find_closest_epoch(target_epoch: int, sorted_epochs: list[int]) -> int | None:
    """Binary search for the candle epoch closest to (but not after) the target."""
    import bisect
    idx = bisect.bisect_right(sorted_epochs, target_epoch)
    if idx == 0:
        return None
    return sorted_epochs[idx - 1]


def compute_broader_trend(entry_epoch: int, epoch_to_close: dict, sorted_epochs: list[int]) -> str | None:
    """
    Returns 'up', 'down', or None (insufficient data) for the broader
    trend over TREND_LOOKBACK_MINUTES before entry_epoch, using the
    real cached candle closes.
    """
    lookback_epoch = entry_epoch - TREND_LOOKBACK_MINUTES * 60

    current_close_epoch = find_closest_epoch(entry_epoch, sorted_epochs)
    past_close_epoch = find_closest_epoch(lookback_epoch, sorted_epochs)

    if current_close_epoch is None or past_close_epoch is None:
        return None
    if current_close_epoch == past_close_epoch:
        return None  # not enough real elapsed data (e.g. very start of the dataset)

    current_price = epoch_to_close[current_close_epoch]
    past_price = epoch_to_close[past_close_epoch]

    return "up" if current_price > past_price else "down"


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/trend_alignment_analysis.py <path_to_trade_log.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        log(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    log(f"Loaded {len(trades)} trades from {csv_path}")

    candles = load_candles()
    epoch_to_close = build_epoch_index(candles)
    sorted_epochs = sorted(epoch_to_close.keys())

    with_trend = []
    against_trend = []
    no_data = 0

    for t in trades:
        entry_dt = parse_trade_datetime(t["Entry Date"], t["Entry Time"])
        entry_epoch = int(entry_dt.timestamp())

        broader_trend = compute_broader_trend(entry_epoch, epoch_to_close, sorted_epochs)
        if broader_trend is None:
            no_data += 1
            continue

        trade_direction = "up" if t["Direction"] == "BULLISH" else "down"
        pnl = float(t["P&L"])

        if trade_direction == broader_trend:
            with_trend.append(pnl)
        else:
            against_trend.append(pnl)

    log(f"\nTrades with insufficient lookback data (skipped): {no_data}")
    log(f"Trades WITH the broader {TREND_LOOKBACK_MINUTES}-min trend: {len(with_trend)}")
    log(f"Trades AGAINST the broader {TREND_LOOKBACK_MINUTES}-min trend: {len(against_trend)}")

    print("\n" + "=" * 70)
    print(f"TREND ALIGNMENT ANALYSIS (lookback window: {TREND_LOOKBACK_MINUTES} minutes)")
    print("=" * 70)

    for label, group in [("WITH the broader trend", with_trend), ("AGAINST the broader trend", against_trend)]:
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

    if with_trend and against_trend:
        wr_with = len([p for p in with_trend if p > 0]) / len(with_trend) * 100
        wr_against = len([p for p in against_trend if p > 0]) / len(against_trend) * 100
        avg_with = sum(with_trend) / len(with_trend)
        avg_against = sum(against_trend) / len(against_trend)

        print("\n" + "-" * 70)
        print("VERDICT:")
        if wr_with > wr_against + 5 and avg_with > avg_against:
            print(f"  Trades WITH the trend meaningfully outperform (win rate +{wr_with-wr_against:.1f}pp, "
                  f"avg P&L {avg_with:.0f} vs {avg_against:.0f}).")
            print("  This SUPPORTS your observation — a higher-timeframe trend filter would likely help.")
        elif wr_against > wr_with + 5 and avg_against > avg_with:
            print(f"  Trades AGAINST the trend actually perform better here (win rate +{wr_against-wr_with:.1f}pp).")
            print("  This DOES NOT support adding a simple trend-following filter — the current")
            print("  mean-reversion-based logic may actually be working as intended, not against you.")
        else:
            print("  No large, clear difference between the two groups in this data.")
            print("  A higher-timeframe filter may not meaningfully change results as currently designed.")


if __name__ == "__main__":
    main()
