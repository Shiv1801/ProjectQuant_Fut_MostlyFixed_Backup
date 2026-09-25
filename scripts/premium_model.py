"""
Simplified option premium estimator for backtesting, replacing the
previous hardcoded constant (Rs 60 for every single trade regardless
of market conditions) — a real, confirmed flaw that made every
backtest's P&L numbers untrustworthy (verified: 2,523/2,523 trades in
the 3-year Nifty test all showed an identical entry premium).

Uses a standard Black-Scholes approximation for a European option,
driven by REAL inputs available at each point in the backtest:
  - spot price (the actual index level at that candle)
  - strike (the selected strike)
  - time to expiry (assumed weekly expiry, matching Nifty's real
    weekly options — the days remaining until the next Thursday)
  - realized volatility computed from the actual trailing price
    window at that point in time (already-built Group A model),
    used as an IV proxy since real historical IV isn't available
  - risk-free rate (a fixed, reasonable approximation)

This is still an approximation — it does not use real implied
volatility, since no historical option-chain data exists for that
(this is the same, previously-documented data limitation as always).
But unlike the old constant-Rs-60 model, premiums now genuinely vary
with market conditions, which is the minimum bar for a backtest's P&L
numbers to be meaningfully trustworthy rather than an artifact of a
frozen constant.
"""
import math
from datetime import datetime, timedelta

RISK_FREE_RATE = 0.065  # approximate Indian short-term risk-free rate


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes_premium(spot: float, strike: float, days_to_expiry: float,
                           volatility_pct: float, option_type: str,
                           risk_free_rate: float = RISK_FREE_RATE) -> float:
    """
    Standard Black-Scholes premium for a European call/put.
    volatility_pct: annualized volatility as a percentage (e.g. 12.5 for 12.5%).
    days_to_expiry: calendar days remaining (converted to years internally).
    Returns the theoretical premium, floored at 0.05 (options never
    price at exactly zero in practice, and a hard zero would break
    percentage-based P&L calculations downstream).
    """
    if days_to_expiry <= 0:
        # At/past expiry: intrinsic value only.
        if option_type == "CE":
            return max(0.05, spot - strike)
        else:
            return max(0.05, strike - spot)

    T = days_to_expiry / 365.0
    sigma = max(volatility_pct, 1.0) / 100.0  # floor to avoid a degenerate near-zero-vol blowup
    sqrtT = math.sqrt(T)

    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    if option_type == "CE":
        premium = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * T) * _norm_cdf(d2)
    else:
        premium = strike * math.exp(-risk_free_rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

    return max(0.05, round(premium, 2))


def days_to_next_weekly_expiry(current_dt: datetime) -> float:
    """
    Nifty weekly options expire on Thursday. Returns the number of
    calendar days remaining until the next Thursday (0 if today IS
    Thursday, treated as same-day expiry pricing).
    """
    THURSDAY = 3  # Monday=0 ... Sunday=6
    days_ahead = (THURSDAY - current_dt.weekday()) % 7
    return float(days_ahead)
