"""
EMA 10/20 Crossover Alignment Test — tests whether your real trades
align with a classic 10-period/20-period EMA crossover signal (fast
EMA above slow EMA = bullish bias, fast below slow = bearish bias).

Excludes trades with Entry premium < Rs 10 UPFRONT (not after the
fact) — these are the confirmed low-premium/high-leverage outlier
trades that distorted earlier tests (57% of total 3-year P&L came from
just 6.4% of trades, all entering near-worthless, near-expiry
premiums). Excluding them from the start means every number in this
report reflects the real, general-case behavior of the strategy, not
a handful of extreme leveraged bets.

Usage:
    python scripts/test_ema_cross.py <path_to_trade_log.csv>

Requires the cached candles already fetched by nifty_mastery_test.py
(nifty_mastery_cache/nifty_3yr_candles.json) — no new Fyers fetch.
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
FAST_EMA_PERIOD = 10
SLOW_EMA_PERIOD = 20


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


def compute_ema_series(closes: list[float], period: int) -> list[float | None]:
    """Standard EMA: first value seeded as SMA of the first `period`
    closes, then the normal EMA recursion from there. Returns None for
    indices before the series has enough data to compute a real EMA."""
    n = len(closes)
    ema = [None] * n
    if n < period:
        return ema
    multiplier = 2 / (period + 1)
    sma = sum(closes[:period]) / period
    ema[period - 1] = sma
    for i in range(period, n):
        ema[i] = (closes[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_ema_cross.py <path_to_trade_log.csv> [--no-exclude]")
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
    closes = [c["close"] for c in candles]
    epoch_to_index = {c["epoch"]: i for i, c in enumerate(candles)}
    sorted_epochs = sorted(epoch_to_index.keys())

    log(f"Computing EMA-{FAST_EMA_PERIOD} and EMA-{SLOW_EMA_PERIOD}...")
    fast_ema = compute_ema_series(closes, FAST_EMA_PERIOD)
    slow_ema = compute_ema_series(closes, SLOW_EMA_PERIOD)

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

        if entry_index is None or fast_ema[entry_index] is None or slow_ema[entry_index] is None:
            no_signal += 1
            continue

        ema_signal = "bullish" if fast_ema[entry_index] > slow_ema[entry_index] else "bearish"
        trade_direction = "bullish" if t["Direction"] == "BULLISH" else "bearish"
        pnl = float(t["P&L"])

        if trade_direction == ema_signal:
            with_signal.append(pnl)
        else:
            against_signal.append(pnl)

    log(f"\nTrades with no EMA signal available (skipped): {no_signal}")
    log(f"Trades WITH the EMA {FAST_EMA_PERIOD}/{SLOW_EMA_PERIOD} cross signal: {len(with_signal)}")
    log(f"Trades AGAINST the EMA {FAST_EMA_PERIOD}/{SLOW_EMA_PERIOD} cross signal: {len(against_signal)}")

    report_lines = []
    report_lines.append("=" * 70)
    report_lines.append(f"EMA {FAST_EMA_PERIOD}/{SLOW_EMA_PERIOD} CROSSOVER ALIGNMENT TEST")
    if exclude_low_premium:
        report_lines.append(f"(low-premium trades under Rs{LOW_PREMIUM_EXCLUDE_THRESHOLD} excluded upfront: {excluded_count} trades removed)")
    else:
        report_lines.append("(NO EXCLUSIONS -- full trade set including low-premium outliers)")
    report_lines.append("=" * 70)

    summary = {}
    for label, group in [("WITH EMA cross signal", with_signal), ("AGAINST EMA cross signal", against_signal)]:
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
            verdict = "SUPPORTS adding an EMA cross filter"
            report_lines.append(f"  WITH-signal trades meaningfully outperform (win rate +{wr_with-wr_against:.1f}pp, "
                                 f"avg P&L {avg_with:.0f} vs {avg_against:.0f}).")
        elif wr_against > wr_with + 5 and avg_against > avg_with:
            verdict = "DOES NOT support a simple EMA cross filter"
            report_lines.append(f"  AGAINST-signal trades actually perform better (win rate +{wr_against-wr_with:.1f}pp).")
        else:
            verdict = "No clear difference"
            report_lines.append("  No large, clear difference between the two groups in this (cleaned) data.")
        report_lines.append(f"\n  {verdict}")
        summary["verdict"] = verdict

    report_text = "\n".join(report_lines)
    print("\n" + report_text)

    mode_suffix = "_excluded" if exclude_low_premium else "_full"
    out_path = OUTPUT_DIR / f"ema_cross_test_result{mode_suffix}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    with open(OUTPUT_DIR / f"ema_cross_test_result{mode_suffix}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    log(f"\nResults written to {out_path} and .json")


if __name__ == "__main__":
    main()
