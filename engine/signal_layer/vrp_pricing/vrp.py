"""
Group B: Variance Risk Premium = Implied Vol - Forecasted Realized Vol.
PRIMARY buy/no-buy filter per PRD §3.5-B: only favorable to buy options
when premium is fairly priced or cheap (VRP low/negative), never when
options are richly priced relative to what the market is likely to
realize (VRP high).
"""
def compute_vrp(atm_iv_pct: float | None, garch_forecast_vol_pct: float | None,
                 vrp_buy_threshold_pct: float = 2.0) -> dict:
    """
    atm_iv_pct: ATM option's implied vol, annualized %, from black_scholes.implied_volatility()
    garch_forecast_vol_pct: forward-looking realized vol forecast, annualized %, from garch.py
    vrp_buy_threshold_pct: VRP below this is considered favorable to buy options
    """
    if atm_iv_pct is None or garch_forecast_vol_pct is None:
        return {
            "vrp_pct": None, "favorable": None,
            "note": "missing IV or GARCH forecast — cannot compute VRP",
        }

    vrp = atm_iv_pct - garch_forecast_vol_pct
    favorable = vrp <= vrp_buy_threshold_pct

    return {
        "vrp_pct": round(vrp, 2),
        "atm_iv_pct": round(atm_iv_pct, 2),
        "garch_forecast_vol_pct": round(garch_forecast_vol_pct, 2),
        "favorable": favorable,
        "note": "options fairly/cheaply priced — favorable to buy" if favorable
                else "options richly priced relative to forecast — unfavorable to buy",
    }
