"""Group A: EWMA volatility estimator.

Returns annualized volatility in percentage points, consistent with the
Realized Volatility, Parkinson, Garman-Klass, and GARCH model outputs.
"""

import math

import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375
ANNUALIZATION_FACTOR = math.sqrt(
    TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION
)


class EwmaVolatility(QuantModel):
    model_id = "ewma_vol"
    group = "A"
    lam = 0.94  # Conventional RiskMetrics decay factor; validate for horizon.

    def compute(self, market_data: dict) -> ModelOutput:
        try:
            closes = np.asarray(market_data["close_1min"], dtype=float)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{self.model_id}: missing or invalid close data"
            ) from exc

        if closes.ndim != 1:
            raise ValueError(f"{self.model_id}: closes must be a 1-D array")

        if closes.size < 3:
            raise ValueError(
                f"{self.model_id}: at least 3 candle closes are required"
            )

        if not np.all(np.isfinite(closes)):
            raise ValueError(
                f"{self.model_id}: closes contain NaN or infinity"
            )

        if np.any(closes <= 0):
            raise ValueError(
                f"{self.model_id}: prices must be strictly positive"
            )

        if not 0.0 < self.lam < 1.0:
            raise ValueError(
                f"{self.model_id}: lambda must be between 0 and 1"
            )

        log_returns = np.diff(np.log(closes))

        if log_returns.size < 2 or not np.all(np.isfinite(log_returns)):
            raise ValueError(
                f"{self.model_id}: insufficient or invalid log returns"
            )

        # EWMA variance recursion, initialized with the first squared return.
        variance = float(log_returns[0] ** 2)
        for ret in log_returns[1:]:
            variance = (
                self.lam * variance
                + (1.0 - self.lam) * float(ret * ret)
            )

        if not math.isfinite(variance) or variance < 0.0:
            raise ValueError(
                f"{self.model_id}: invalid variance estimate"
            )

        # Log returns are decimals, so output is percentage points.
        annualized_vol_pct = (
            math.sqrt(variance) * ANNUALIZATION_FACTOR * 100.0
        )

        if not math.isfinite(annualized_vol_pct):
            raise ValueError(
                f"{self.model_id}: non-finite volatility estimate"
            )

        return ModelOutput(
            self.model_id,
            score=annualized_vol_pct,
            confidence=None,
            feature_vector={"ewma_vol": annualized_vol_pct},
        )
