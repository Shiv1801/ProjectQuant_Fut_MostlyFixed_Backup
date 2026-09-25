"""
Volume Data Quality Investigation — checks, year by year, whether real
volume data exists in the cached candle history, to explain why VWAP
produced ZERO usable signals for 2021-2024 but worked fine in 2025-2026
(found by diagnose_zero_trades.py).

Reports, per year: what fraction of candles have zero/missing volume,
the average volume where present, and specifically checks the FIRST
candle of each trading day (since VWAP resets daily and starts as None
until the first non-zero-volume candle of that day appears -- if EVERY
candle in a day has zero volume, VWAP never initializes for that whole
day).

Usage:
    python scripts/investigate_volume_data.py

Requires the 5-year cache already built by vwap_5yr_detailed_log.py.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("vwap_5yr_cache/nifty_5yr_candles.json")


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_candles() -> list[dict]:
    if not CACHE_FILE.exists():
        log(f"ERROR: {CACHE_FILE} not found. Run vwap_5yr_detailed_log.py first.")
        sys.exit(1)
    with open(CACHE_FILE) as f:
        candles = json.load(f)
    log(f"Loaded {len(candles)} cached candles.")
    return candles


def main():
    candles = load_candles()

    # Per-year stats
    year_stats = defaultdict(lambda: {
        "total_candles": 0, "zero_volume_candles": 0, "volume_sum": 0,
        "days_seen": set(), "days_with_any_real_volume": set(),
    })

    for c in candles:
        dt = datetime.fromtimestamp(c["epoch"], tz=IST)
        year = dt.year
        day_key = dt.date()
        volume = c.get("volume", 0) or 0

        year_stats[year]["total_candles"] += 1
        year_stats[year]["days_seen"].add(day_key)
        if volume == 0:
            year_stats[year]["zero_volume_candles"] += 1
        else:
            year_stats[year]["volume_sum"] += volume
            year_stats[year]["days_with_any_real_volume"].add(day_key)

    print("\n" + "=" * 100)
    print("VOLUME DATA QUALITY BY YEAR")
    print("=" * 100)
    print(f"{'Year':<6} {'Total Candles':<15} {'Zero-Vol Candles':<18} {'% Zero Vol':<12} "
          f"{'Trading Days':<14} {'Days w/ ANY Real Vol':<22} {'Avg Vol (non-zero)'}")
    print("-" * 100)

    for year in sorted(year_stats.keys()):
        s = year_stats[year]
        pct_zero = s["zero_volume_candles"] / s["total_candles"] * 100 if s["total_candles"] > 0 else 0
        non_zero_count = s["total_candles"] - s["zero_volume_candles"]
        avg_vol = s["volume_sum"] / non_zero_count if non_zero_count > 0 else 0
        days_total = len(s["days_seen"])
        days_with_real_vol = len(s["days_with_any_real_volume"])

        print(f"{year:<6} {s['total_candles']:<15} {s['zero_volume_candles']:<18} {pct_zero:<12.1f} "
              f"{days_total:<14} {days_with_real_vol:<22} {avg_vol:,.0f}")

    print("\n" + "-" * 100)
    print("INTERPRETATION:")
    print("If 'Days w/ ANY Real Vol' is much lower than 'Trading Days' for 2021-2024,")
    print("entire trading days in those years have ZERO real volume data -- VWAP can")
    print("never initialize on those days (it starts as None until the first non-zero-")
    print("volume candle), which would fully explain the zero-signal result found earlier.")
    print("\nIf 'Days w/ ANY Real Vol' is close to 'Trading Days' but '% Zero Vol' is still")
    print("high, volume exists but is SPARSE within each day -- a different, more subtle issue.")

    # Also show a concrete example: the very first candle of a specific day each year,
    # to make the pattern visible and checkable by eye, not just aggregate stats.
    print("\n" + "=" * 100)
    print("SAMPLE: first 3 candles of the first trading day found in each year")
    print("=" * 100)
    shown_years = set()
    day_candle_buffer = []
    last_day = None
    for c in candles:
        dt = datetime.fromtimestamp(c["epoch"], tz=IST)
        year = dt.year
        day_key = dt.date()
        if year not in shown_years:
            if last_day != day_key:
                day_candle_buffer = []
                last_day = day_key
            day_candle_buffer.append((dt, c.get("volume", 0)))
            if len(day_candle_buffer) >= 3:
                print(f"\n{year} -- {day_key}:")
                for sample_dt, vol in day_candle_buffer:
                    print(f"  {sample_dt.strftime('%H:%M')}  volume={vol}")
                shown_years.add(year)
                day_candle_buffer = []


if __name__ == "__main__":
    main()
