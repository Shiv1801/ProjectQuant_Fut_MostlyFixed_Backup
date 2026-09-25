"""
Group A: GARCH(1,1) one-step-ahead conditional volatility forecast
(PRD §3.5-A). Forward-looking vol estimate — this is what Group B's
Variance Risk Premium model will compare against implied vol.

Needs a reasonable amount of history to fit meaningfully (rule of thumb:
at least ~30 data points, ideally 100+). With too little data we return
a clear "insufficient data" response rather than a garbage number.
"""
import numpy as np

MIN_CANDLES_FOR_GARCH = 30


def compute_garch_forecast(closes: list[float]) -> dict:
    if len(closes) < MIN_CANDLES_FOR_GARCH:
        return {
            "garch_forecast_vol_pct": None,
            "sample_size": len(closes),
            "note": f"need at least {MIN_CANDLES_FOR_GARCH} candles, have {len(closes)}",
        }

    try:
        from arch import arch_model
    except ImportError:
        return {"garch_forecast_vol_pct": None, "sample_size": len(closes), "note": "arch package not installed"}

    closes_arr = np.array(closes)
    returns = 100 * np.diff(np.log(closes_arr))  # percent log returns, arch library convention

    if np.std(returns) == 0:
        return {"garch_forecast_vol_pct": 0.0, "sample_size": len(closes), "note": "flat price series"}

    try:
        am = arch_model(returns, vol="Garch", p=1, q=1, dist="normal", rescale=False)
        res = am.fit(disp="off", show_warning=False)
        forecast = res.forecast(horizon=1)
        next_var = float(forecast.variance.values[-1, 0])
        # Convert 1-min variance forecast to annualized vol %
        forecast_vol_annualized = (next_var ** 0.5) * np.sqrt(252 * 375)
        return {
            "garch_forecast_vol_pct": round(float(forecast_vol_annualized), 2),
            "sample_size": len(closes),
            "note": None,
        }
    except Exception as e:
        return {"garch_forecast_vol_pct": None, "sample_size": len(closes), "note": f"fit failed: {str(e)}"}
