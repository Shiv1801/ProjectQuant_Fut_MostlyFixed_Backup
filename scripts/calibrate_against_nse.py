"""
Backtest Calibration Tool — for any trade in a backtest trade log CSV,
fetches the REAL NSE end-of-day option price for that exact strike,
expiry, and date (via the jugaad-data library, which pulls directly
from nseindia.com), and reports how far off our Black-Scholes model
was that day.

IMPORTANT, HONEST LIMITATION: NSE's official data is END-OF-DAY only
(Open/High/Low/Close/Settlement per contract per day) -- there is no
free source of real INTRADAY option prices. This tool cannot make the
backtest itself use real intraday prices; it can only tell you, for
each trading DAY, how our model's premium compared to that day's real
Open/Close -- a genuine, honest calibration check, not a full fix.

Setup (one-time):
    pip install jugaad-data --break-system-packages

Usage:
    python scripts/calibrate_against_nse.py <path_to_trade_log.csv>

Output: a report showing, for each unique (date, strike, option_type)
in your trade log, the real NSE Open/High/Low/Close/Settle alongside
what our model priced that trade's entry and exit at, plus the %
difference -- so you can see concretely how biased the model is.
"""
import sys
import csv
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

try:
    from jugaad_data.nse import derivatives_df
except ImportError:
    print("ERROR: jugaad-data is not installed. Run:")
    print("  pip install jugaad-data --break-system-packages")
    sys.exit(1)

OUTPUT_DIR = Path("sample_test")
NIFTY_LOT_SIZE = 25


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def parse_trade_date(date_str: str) -> "datetime.date":
    """Parses the mastery-test CSV's 'DD Mon YY' date format."""
    return datetime.strptime(date_str, "%d %b %y").date()


def next_weekly_expiry(trade_date) -> "datetime.date":
    """
    Nifty weekly options expire on Thursday. Returns the Thursday ON
    OR AFTER trade_date (if trade_date IS a Thursday, that's the expiry
    -- matches how the app's own days_to_next_weekly_expiry logic
    treats same-day Thursday).
    """
    THURSDAY = 3
    days_ahead = (THURSDAY - trade_date.weekday()) % 7
    return trade_date + timedelta(days=days_ahead)


def fetch_real_price(trade_date, strike: float, option_type: str, expiry) -> dict | None:
    """
    Fetches the real NSE end-of-day price for one specific NIFTY option
    contract on one specific date. Returns None if NSE has no data for
    that combination (contract didn't exist / no trades that day /
    date too far in the future).
    """
    try:
        df = derivatives_df(
            symbol="NIFTY", from_date=trade_date, to_date=trade_date,
            expiry_date=expiry, instrument_type="OPTIDX",
            strike_price=strike, option_type=option_type,
        )
    except Exception as e:
        return {"error": str(e)}

    if df is None or len(df) == 0:
        return None

    row = df.iloc[0]
    return {
        "open": float(row["OPEN"]), "high": float(row["HIGH"]), "low": float(row["LOW"]),
        "close": float(row["CLOSE"]), "ltp": float(row["LTP"]), "settle": float(row["SETTLE PRICE"]),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/calibrate_against_nse.py <path_to_trade_log.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        log(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    log(f"Loaded {len(trades)} trades from {csv_path}")

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Deduplicate to unique (date, strike, option_type) combinations --
    # no point fetching the same contract's price twice, and this keeps
    # the number of real NSE requests (and the risk of rate-limiting)
    # to the minimum genuinely needed.
    unique_lookups = {}
    for t in trades:
        entry_date = parse_trade_date(t["Entry Date"])
        option_type = "CE" if t["Direction"] == "BULLISH" else "PE"
        strike = float(t["Strike"])
        key = (entry_date, strike, option_type)
        if key not in unique_lookups:
            unique_lookups[key] = {"trade_refs": [], "model_entry_premium": float(t["Entry ₹"])}
        unique_lookups[key]["trade_refs"].append(t)

    log(f"Deduplicated to {len(unique_lookups)} unique (date, strike, type) contracts to check against NSE.")

    results = []
    fetch_failures = 0

    for i, ((trade_date, strike, option_type), info) in enumerate(unique_lookups.items()):
        expiry = next_weekly_expiry(trade_date)
        real = fetch_real_price(trade_date, strike, option_type, expiry)

        if real is None or "error" in real:
            fetch_failures += 1
            if (i + 1) % 20 == 0:
                log(f"  ...checked {i+1}/{len(unique_lookups)} (no NSE data or error for some)")
            continue

        model_premium = info["model_entry_premium"]
        real_open = real["open"]
        pct_diff_vs_open = (model_premium - real_open) / real_open * 100 if real_open > 0 else None

        results.append({
            "date": trade_date.strftime("%d-%b-%Y"), "strike": strike, "option_type": option_type,
            "model_premium": round(model_premium, 2),
            "nse_open": real["open"], "nse_high": real["high"], "nse_low": real["low"],
            "nse_close": real["close"], "nse_settle": real["settle"],
            "pct_diff_vs_open": round(pct_diff_vs_open, 1) if pct_diff_vs_open is not None else None,
        })

        if (i + 1) % 20 == 0:
            log(f"  ...checked {i+1}/{len(unique_lookups)}")

    log(f"\nFetch complete. {len(results)} contracts matched against real NSE data, {fetch_failures} had no NSE data available.")

    out_path = OUTPUT_DIR / "nse_calibration_report.csv"
    if results:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        log(f"Full calibration report written to {out_path}")

        diffs = [r["pct_diff_vs_open"] for r in results if r["pct_diff_vs_open"] is not None]
        if diffs:
            avg_diff = sum(diffs) / len(diffs)
            avg_abs_diff = sum(abs(d) for d in diffs) / len(diffs)
            over_estimates = len([d for d in diffs if d > 0])
            under_estimates = len([d for d in diffs if d < 0])

            print("\n" + "=" * 70)
            print("CALIBRATION SUMMARY: model premium vs real NSE day-open price")
            print("=" * 70)
            print(f"Contracts checked: {len(diffs)}")
            print(f"Average % difference (model - real, signed): {avg_diff:+.1f}%")
            print(f"Average ABSOLUTE % difference: {avg_abs_diff:.1f}%")
            print(f"Model overestimated: {over_estimates} times, underestimated: {under_estimates} times")
            print(f"\n(A large average absolute difference means the model's premiums, even")
            print(f" after the same-day-volatility fix, still don't closely track real NSE")
            print(f" prices -- expected, since we lack real implied volatility / skew data.)")
    else:
        log("No results -- either NSE had no data for any of these contracts, or all fetches failed.")


if __name__ == "__main__":
    main()
