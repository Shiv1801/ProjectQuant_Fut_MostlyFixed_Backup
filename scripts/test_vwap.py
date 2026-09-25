"""
VWAP (Volume-Weighted Average Price) Alignment Test — tests whether
your real trades align with price being above VWAP (bullish bias) or
below VWAP (bearish bias). VWAP resets each trading day (standard
convention), computed from real candle volume data.

Same low-premium exclusion and methodology as test_ema_cross.py, for
direct comparability across all three indicator tests.

Usage:
    python scripts/test_vwap.py <path_to_trade_log.csv>

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


def compute_vwap_series(candles: list[dict]) -> list[float | None]:
    """
    Standard VWAP, resetting at the start of each real IST trading day
    (not just every 375 candles, since some days have gaps/holidays —
    resetting on actual calendar-day change is more correct). Uses
    typical price (H+L+C)/3 weighted by volume, the standard formula.
    Falls back gracefully (VWAP = None) if a candle has zero cumulative
    volume so far (first candle of the day, before any volume accrues).
    """
    n = len(candles)
    vwap = [None] * n
    if n == 0:
        return vwap

    cumulative_pv = 0.0
    cumulative_vol = 0.0
    current_day = None

    for i, c in enumerate(candles):
        candle_day = datetime.fromtimestamp(c["epoch"], tz=IST).date()
        if candle_day != current_day:
            current_day = candle_day
            cumulative_pv = 0.0
            cumulative_vol = 0.0

        volume = c.get("volume", 0) or 0
        typical_price = (c["high"] + c["low"] + c["close"]) / 3

        cumulative_pv += typical_price * volume
        cumulative_vol += volume

        vwap[i] = (cumulative_pv / cumulative_vol) if cumulative_vol > 0 else None

    return vwap


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_vwap.py <path_to_trade_log.csv> [--no-exclude]")
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

    log("Computing VWAP (resets each real trading day)...")
    vwap_series = compute_vwap_series(candles)

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

        if entry_index is None or vwap_series[entry_index] is None:
            no_signal += 1
            continue

        current_price = candles[entry_index]["close"]
        vwap_signal = "bullish" if current_price > vwap_series[entry_index] else "bearish"
        trade_direction = "bullish" if t["Direction"] == "BULLISH" else "bearish"
        pnl = float(t["P&L"])

        if trade_direction == vwap_signal:
            with_signal.append(pnl)
        else:
            against_signal.append(pnl)

    log(f"\nTrades with no VWAP signal available (skipped): {no_signal}")
    log(f"Trades WITH the VWAP signal (price above/below VWAP agrees with trade direction): {len(with_signal)}")
    log(f"Trades AGAINST the VWAP signal: {len(against_signal)}")

    report_lines = []
    report_lines.append("=" * 70)
    report_lines.append("VWAP ALIGNMENT TEST")
    if exclude_low_premium:
        report_lines.append(f"(low-premium trades under Rs{LOW_PREMIUM_EXCLUDE_THRESHOLD} excluded upfront: {excluded_count} trades removed)")
    else:
        report_lines.append("(NO EXCLUSIONS -- full trade set including low-premium outliers)")
    report_lines.append("=" * 70)

    summary = {}
    for label, group in [("WITH VWAP signal", with_signal), ("AGAINST VWAP signal", against_signal)]:
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
            verdict = "SUPPORTS adding a VWAP filter"
            report_lines.append(f"  WITH-signal trades meaningfully outperform (win rate +{wr_with-wr_against:.1f}pp, "
                                 f"avg P&L {avg_with:.0f} vs {avg_against:.0f}).")
        elif wr_against > wr_with + 5 and avg_against > avg_with:
            verdict = "DOES NOT support a simple VWAP filter"
            report_lines.append(f"  AGAINST-signal trades actually perform better (win rate +{wr_against-wr_with:.1f}pp).")
        else:
            verdict = "No clear difference"
            report_lines.append("  No large, clear difference between the two groups in this (cleaned) data.")
        report_lines.append(f"\n  {verdict}")
        summary["verdict"] = verdict

    report_text = "\n".join(report_lines)
    print("\n" + report_text)

    mode_suffix = "_excluded" if exclude_low_premium else "_full"
    out_path = OUTPUT_DIR / f"vwap_test_result{mode_suffix}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    with open(OUTPUT_DIR / f"vwap_test_result{mode_suffix}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    log(f"\nResults written to {out_path} and .json")


if __name__ == "__main__":
    main()
