"""IST market-hours helpers used across data layer, confluence gate, and expiry logic."""
from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def minutes_to_close(dt: datetime | None = None) -> float:
    dt = dt or now_ist()
    close_dt = dt.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0)
    return max(0.0, (close_dt - dt).total_seconds() / 60)
