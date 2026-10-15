"""Group A: Parkinson high-low range volatility estimator.

Output convention:
    score and feature_vector["parkinson_vol"] are annualized volatility
    in percentage points, matching realized_vol_pct and GARCH output.

Assumptions:
    - Input candles are ordered consistently.
    - Each observation represents one regular-session one-minute candle.
    - Timestamp continuity is validated by the caller because this model
      receives price arrays only.
"""

import math

import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375
ANNUALIZATION_FACTOR = math.sqrt(
    TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION
)


class ParkinsonEstimator(QuantModel):
    model_id = "parkinson_estimator"
    group = "A"

    def compute(self, market_data: dict) -> ModelOutput:
        try:
            highs = np.asarray(market_data["high_1min"], dtype=float)
            lows = np.asarray(market_data["low_1min"], dtype=float)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{self.model_id}: missing or invalid high/low data"
            ) from exc

        if highs.ndim != 1 or lows.ndim != 1:
            raise ValueError(
                f"{self.model_id}: high and low inputs must be 1-D arrays"
            )

        if highs.size != lows.size:
            raise ValueError(
                f"{self.model_id}: high and low arrays have different lengths"
            )

        if highs.size < 2:
            raise ValueError(
                f"{self.model_id}: at least 2 candles are required"
            )

        if not np.all(np.isfinite(highs)) or not np.all(np.isfinite(lows)):
            raise ValueError(
                f"{self.model_id}: prices contain NaN or infinite values"
            )

        if np.any(highs <= 0) or np.any(lows <= 0):
            raise ValueError(
                f"{self.model_id}: prices must be strictly positive"
            )

        if np.any(highs < lows):
            raise ValueError(
                f"{self.model_id}: high cannot be below low"
            )

        # Parkinson's per-observation variance estimator.
        log_hl = np.log(highs / lows)
        mean_squared_log_range = float(np.mean(np.square(log_hl)))
        annualized_variance = (
            mean_squared_log_range
            / (4.0 * math.log(2.0))
            * TRADING_DAYS_PER_YEAR
            * MINUTES_PER_SESSION
        )

        # Express volatility as percentage points, not a decimal fraction.
        vol_pct = math.sqrt(annualized_variance) * 100.0

        if not math.isfinite(vol_pct):
            raise ValueError(
                f"{self.model_id}: calculated volatility is not finite"
            )

        return ModelOutput(
            self.model_id,
            score=vol_pct,
            confidence=None,
            feature_vector={"parkinson_vol": vol_pct},
        )
