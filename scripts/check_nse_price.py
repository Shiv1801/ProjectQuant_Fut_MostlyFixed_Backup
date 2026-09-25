"""
Standalone NSE Option Price Checker — quick, direct lookup of a real
historical NIFTY option price for one specific strike, date, and
option type. No trade log needed; just answers "what did this option
actually trade at, that day" using NSE's real, official end-of-day
data (via the jugaad-data library).

HONEST LIMITATION: this gives you the day's Open/High/Low/Close/
Settlement — NSE's free data does not include a specific INTRADAY time
like "3:00 PM" (that would need paid tick-level data, which isn't
available to us). What this CAN tell you: the day's real range and
closing price, which is the closest free, real anchor we have.

Setup (one-time):
    pip install jugaad-data --break-system-packages

Usage:
    python scripts/check_nse_price.py <strike> <CE|PE> <DD-MM-YYYY>

Example:
    python scripts/check_nse_price.py 23400 CE 10-09-2026
"""
import sys
from datetime import datetime, timedelta

try:
    from jugaad_data.nse import derivatives_df
except ImportError:
    print("ERROR: jugaad-data is not installed. Run:")
    print("  pip install jugaad-data --break-system-packages")
    sys.exit(1)


def next_weekly_expiry(trade_date):
    """Nifty weekly options expire on Thursday. Returns the Thursday ON
    OR AFTER trade_date."""
    THURSDAY = 3
    days_ahead = (THURSDAY - trade_date.weekday()) % 7
    return trade_date + timedelta(days=days_ahead)


def main():
    if len(sys.argv) != 4:
        print("Usage: python scripts/check_nse_price.py <strike> <CE|PE> <DD-MM-YYYY>")
        print("Example: python scripts/check_nse_price.py 23400 CE 10-09-2026")
        sys.exit(1)

    strike = float(sys.argv[1])
    option_type = sys.argv[2].upper()
    if option_type not in ("CE", "PE"):
        print(f"ERROR: option type must be CE or PE, got '{option_type}'")
        sys.exit(1)

    try:
        trade_date = datetime.strptime(sys.argv[3], "%d-%m-%Y").date()
    except ValueError:
        print(f"ERROR: could not parse date '{sys.argv[3]}' -- use DD-MM-YYYY format")
        sys.exit(1)

    expiry = next_weekly_expiry(trade_date)
    print(f"Looking up: NIFTY {strike} {option_type}, expiry {expiry}, date {trade_date}")
    print("Fetching from NSE (nseindia.com)...")

    try:
        df = derivatives_df(
            symbol="NIFTY", from_date=trade_date, to_date=trade_date,
            expiry_date=expiry, instrument_type="OPTIDX",
            strike_price=strike, option_type=option_type,
        )
    except Exception as e:
        print(f"\nERROR fetching from NSE: {type(e).__name__}: {e}")
        print("\nPossible causes:")
        print("  - NSE's site structure changed, or the library needs updating (pip install -U jugaad-data)")
        print("  - No internet access from this machine right now")
        print("  - NSE occasionally blocks automated requests temporarily -- try again in a minute")
        sys.exit(1)

    if df is None or len(df) == 0:
        print(f"\nNo NSE data found for this exact contract on {trade_date}.")
        print("Possible reasons: wrong expiry (are you sure this strike existed for the expiry")
        print("computed above?), the contract had zero trades that day, or the date is a holiday/weekend.")
        sys.exit(0)

    row = df.iloc[0]
    print("\n" + "=" * 60)
    print(f"REAL NSE DATA: NIFTY {strike} {option_type}, {trade_date}, expiry {expiry}")
    print("=" * 60)
    print(f"  Open:          {row['OPEN']}")
    print(f"  High:          {row['HIGH']}")
    print(f"  Low:           {row['LOW']}")
    print(f"  Close:         {row['CLOSE']}")
    print(f"  Last Traded:   {row['LTP']}")
    print(f"  Settle Price:  {row['SETTLE PRICE']}")
    print(f"  Volume:        {row['TOTAL TRADED QUANTITY']}")
    print(f"  Open Interest: {row['OPEN INTEREST']}")
    print("\nNote: NSE's free data is end-of-day only -- this is the day's real")
    print("range and closing price, not a specific intraday timestamp like 3:00 PM.")


if __name__ == "__main__":
    main()
