"""Group A: GARCH(1,1) one-step-ahead conditional volatility forecast.

Returns annualized volatility in percentage points, using one-minute
log returns. Forecast quality must be validated out of sample.
"""

import math

import numpy as np

MIN_CANDLES_FOR_GARCH = 30
TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375
ANNUALIZATION_FACTOR = math.sqrt(
    TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION
)


def _failure(sample_size: int, note: str) -> dict:
    return {
        "garch_forecast_vol_pct": None,
        "sample_size": sample_size,
        "note": note,
    }


def compute_garch_forecast(closes: list[float]) -> dict:
    if closes is None:
        return _failure(0, "closes is None")

    try:
        closes_arr = np.asarray(closes, dtype=float)
    except (TypeError, ValueError, OverflowError):
        return _failure(0, "invalid price data: non-numeric value")

    if closes_arr.ndim != 1:
        return _failure(0, "closes must be a one-dimensional sequence")

    if closes_arr.size < MIN_CANDLES_FOR_GARCH:
        return _failure(
            int(closes_arr.size),
            f"need at least {MIN_CANDLES_FOR_GARCH} candles, have {closes_arr.size}",
        )

    if not np.all(np.isfinite(closes_arr)):
        return _failure(int(closes_arr.size), "prices contain NaN or infinity")

    if np.any(closes_arr <= 0):
        return _failure(int(closes_arr.size), "prices must be strictly positive")

    returns = 100.0 * np.diff(np.log(closes_arr))

    if returns.size < MIN_CANDLES_FOR_GARCH or not np.all(np.isfinite(returns)):
        return _failure(int(returns.size), "insufficient or invalid log returns")

    if float(np.std(returns)) <= 1e-12:
        return {
            "garch_forecast_vol_pct": 0.0,
            "sample_size": int(returns.size),
            "note": "flat or near-flat price series",
        }

    try:
        from arch import arch_model
    except ImportError:
        return _failure(
            int(returns.size),
            "arch package not installed",
        )

    try:
        model = arch_model(
            returns,
            mean="Zero",
            vol="GARCH",
            p=1,
            q=1,
            dist="normal",
            rescale=False,
        )
        result = model.fit(disp="off", show_warning=False)

        if not result.convergence_flag == 0:
            return _failure(
                int(returns.size),
                f"GARCH optimizer did not converge (flag={result.convergence_flag})",
            )

        forecast = result.forecast(horizon=1, reindex=False)
        variance = float(forecast.variance.values[-1, 0])

        if not math.isfinite(variance) or variance < 0:
            return _failure(
                int(returns.size),
                "GARCH produced an invalid variance forecast",
            )

        # Returns are expressed in percent, so forecast variance is in
        # squared percentage points. Annualize without multiplying by 100 again.
        annualized_vol_pct = math.sqrt(variance) * ANNUALIZATION_FACTOR

        if not math.isfinite(annualized_vol_pct):
            return _failure(
                int(returns.size),
                "GARCH produced a non-finite volatility forecast",
            )

        return {
            "garch_forecast_vol_pct": round(annualized_vol_pct, 4),
            "sample_size": int(returns.size),
            "note": None,
        }

    except Exception as exc:
        return _failure(
            int(returns.size),
            f"GARCH fit failed: {type(exc).__name__}: {exc}",
        )
