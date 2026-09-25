"""
Ichimoku Tenkan-sen/Kijun-sen Crossover Alignment Test — tests whether
your real trades align with the classic Ichimoku "TK cross" (Tenkan-sen
above Kijun-sen = bullish bias, below = bearish bias). Uses the
standard Ichimoku periods (9 for Tenkan, 26 for Kijun), the same
well-established defaults used across virtually all Ichimoku
implementations, not arbitrary custom values.

Same low-premium exclusion and methodology as the other two indicator
tests, for direct comparability.

Tenkan-sen = (highest high + lowest low) / 2 over the last 9 periods
Kijun-sen  = (highest high + lowest low) / 2 over the last 26 periods

Usage:
    python scripts/test_ichimoku.py <path_to_trade_log.csv>

Requires the cached candles already fetched by nifty_mastery_test.py.
Output written to sample_test/ in the project root.
"""
import sys
import csv
import json
import bisect
from pathlib import Path
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_FILE = Path("nifty_mastery_cache/nifty_3yr_candles.json")
OUTPUT_DIR = Path("sample_test")
LOW_PREMIUM_EXCLUDE_THRESHOLD = 10.0
TENKAN_PERIOD = 9
KIJUN_PERIOD = 26


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


def compute_midpoint_line(candles: list[dict], period: int) -> list[float | None]:
    """Generic (highest high + lowest low) / 2 over a trailing window —
    the shared formula behind both Tenkan-sen and Kijun-sen, just with
    different periods."""
    n = len(candles)
    line = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]
        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        line[i] = (highest + lowest) / 2
    return line


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_ichimoku.py <path_to_trade_log.csv> [--no-exclude]")
        print("  --no-exclude : test on the FULL trade set, with no low-premium exclusion")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = Path(sys.argv[1])
    exclude_low_premium = "--no-exclude" not in sys.argv
    with open(csv_path, encoding="utf-8") as f:
        all_trades = list(csv.DictReader(f))
    log(f"Loaded {len(all_trades)} trades from {csv_path}")

    if exclude_low_premium:
        trades = [t for t in all_trades if float(t["Entry ₹"]) >= LOW_PREMIUM_EXCLUDE_THRESHOLD]
        excluded_count = len(all_trades) - len(trades)
        log(f"Excluded {excluded_count} low-premium (<Rs{LOW_PREMIUM_EXCLUDE_THRESHOLD}) trades upfront.")
        log(f"Testing against {len(trades)} remaining trades.")
    else:
        trades = all_trades
        excluded_count = 0
        log(f"NO EXCLUSIONS — testing against the FULL {len(trades)} trades, including low-premium outliers.")

    candles = load_candles()
    epoch_to_index = {c["epoch"]: i for i, c in enumerate(candles)}
    sorted_epochs = sorted(epoch_to_index.keys())

    log(f"Computing Tenkan-sen ({TENKAN_PERIOD}) and Kijun-sen ({KIJUN_PERIOD})...")
    tenkan = compute_midpoint_line(candles, TENKAN_PERIOD)
    kijun = compute_midpoint_line(candles, KIJUN_PERIOD)

    def find_candle_index(target_epoch: int):
        idx = bisect.bisect_right(sorted_epochs, target_epoch)
        if idx == 0:
            return None
        return epoch_to_index[sorted_epochs[idx - 1]]

    with_signal = []
    against_signal = []
    no_signal = 0

    for t in trades:
        entry_dt = parse_trade_datetime(t["Entry Date"], t["Entry Time"])
        entry_index = find_candle_index(int(entry_dt.timestamp()))

        if entry_index is None or tenkan[entry_index] is None or kijun[entry_index] is None:
            no_signal += 1
            continue

        tk_signal = "bullish" if tenkan[entry_index] > kijun[entry_index] else "bearish"
        trade_direction = "bullish" if t["Direction"] == "BULLISH" else "bearish"
        pnl = float(t["P&L"])

        if trade_direction == tk_signal:
            with_signal.append(pnl)
        else:
            against_signal.append(pnl)

    log(f"\nTrades with no Ichimoku signal available (skipped): {no_signal}")
    log(f"Trades WITH the Tenkan/Kijun cross signal: {len(with_signal)}")
    log(f"Trades AGAINST the Tenkan/Kijun cross signal: {len(against_signal)}")

    report_lines = []
    report_lines.append("=" * 70)
    report_lines.append(f"ICHIMOKU TENKAN({TENKAN_PERIOD})/KIJUN({KIJUN_PERIOD}) CROSS ALIGNMENT TEST")
    if exclude_low_premium:
        report_lines.append(f"(low-premium trades under Rs{LOW_PREMIUM_EXCLUDE_THRESHOLD} excluded upfront: {excluded_count} trades removed)")
    else:
        report_lines.append("(NO EXCLUSIONS -- full trade set including low-premium outliers)")
    report_lines.append("=" * 70)

    summary = {}
    for label, group in [("WITH Tenkan/Kijun cross signal", with_signal), ("AGAINST Tenkan/Kijun cross signal", against_signal)]:
        if not group:
            report_lines.append(f"\n{label}: no trades in this group.")
            continue
        wins = [p for p in group if p > 0]
        total_pnl = sum(group)
        win_rate = len(wins) / len(group) * 100
        avg_pnl = total_pnl / len(group)
        report_lines.append(f"\n{label}:")
        report_lines.append(f"  Trades: {len(group)}")
        report_lines.append(f"  Win rate: {win_rate:.1f}%")
        report_lines.append(f"  Total P&L: {total_pnl:,.2f}")
        report_lines.append(f"  Avg P&L per trade: {avg_pnl:,.2f}")
        summary[label] = {"trades": len(group), "win_rate": round(win_rate, 1), "total_pnl": round(total_pnl, 2), "avg_pnl": round(avg_pnl, 2)}

    if with_signal and against_signal:
        wr_with = len([p for p in with_signal if p > 0]) / len(with_signal) * 100
        wr_against = len([p for p in against_signal if p > 0]) / len(against_signal) * 100
        avg_with = sum(with_signal) / len(with_signal)
        avg_against = sum(against_signal) / len(against_signal)

        report_lines.append("\n" + "-" * 70)
        report_lines.append("VERDICT:")
        if wr_with > wr_against + 5 and avg_with > avg_against:
            verdict = "SUPPORTS adding a Tenkan/Kijun cross filter"
            report_lines.append(f"  WITH-signal trades meaningfully outperform (win rate +{wr_with-wr_against:.1f}pp, "
                                 f"avg P&L {avg_with:.0f} vs {avg_against:.0f}).")
        elif wr_against > wr_with + 5 and avg_against > avg_with:
            verdict = "DOES NOT support a simple Tenkan/Kijun cross filter"
            report_lines.append(f"  AGAINST-signal trades actually perform better (win rate +{wr_against-wr_with:.1f}pp).")
        else:
            verdict = "No clear difference"
            report_lines.append("  No large, clear difference between the two groups in this (cleaned) data.")
        report_lines.append(f"\n  {verdict}")
        summary["verdict"] = verdict

    report_text = "\n".join(report_lines)
    print("\n" + report_text)

    mode_suffix = "_excluded" if exclude_low_premium else "_full"
    out_path = OUTPUT_DIR / f"ichimoku_test_result{mode_suffix}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    with open(OUTPUT_DIR / f"ichimoku_test_result{mode_suffix}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    log(f"\nResults written to {out_path} and .json")


if __name__ == "__main__":
    main()
