import math
import numpy as np

from engine.signal_layer.base_model import ModelOutput, QuantModel

TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375
ANNUALIZATION_FACTOR = math.sqrt(TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION)


class GarmanKlassEstimator(QuantModel):
    """Garman–Klass annualized volatility estimator from one-minute OHLC bars.

    Expected market_data keys: open_1min, high_1min, low_1min, close_1min.
    Output score is annualized volatility in percentage points (e.g. 12.5 = 12.5%).
    """

    model_id = "garman_klass_estimator"
    group = "A"

    def compute(self, market_data: dict) -> ModelOutput:
        keys = ("open_1min", "high_1min", "low_1min", "close_1min")
        if not isinstance(market_data, dict):
            raise ValueError(f"{self.model_id}: market_data must be a dictionary")

        try:
            prices = {
                key: np.asarray(market_data[key], dtype=float)
                for key in keys
            }
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{self.model_id}: missing or invalid OHLC data"
            ) from exc

        if any(values.ndim != 1 for values in prices.values()):
            raise ValueError(f"{self.model_id}: OHLC inputs must be 1-D arrays")

        lengths = {values.size for values in prices.values()}
        if len(lengths) != 1:
            raise ValueError(f"{self.model_id}: OHLC arrays have different lengths")

        n = next(iter(lengths))
        if n < 2:
            raise ValueError(f"{self.model_id}: at least 2 candles are required")

        if any(not np.all(np.isfinite(values)) for values in prices.values()):
            raise ValueError(f"{self.model_id}: OHLC data contains NaN or infinity")

        if any(np.any(values <= 0) for values in prices.values()):
            raise ValueError(f"{self.model_id}: all OHLC prices must be positive")

        o = prices["open_1min"]
        h = prices["high_1min"]
        l = prices["low_1min"]
        c = prices["close_1min"]

        if np.any(h < l):
            raise ValueError(f"{self.model_id}: high cannot be below low")
        if np.any(h < np.maximum(o, c)):
            raise ValueError(f"{self.model_id}: high must be >= open and close")
        if np.any(l > np.minimum(o, c)):
            raise ValueError(f"{self.model_id}: low must be <= open and close")

        log_hl = np.log(h / l)
        log_co = np.log(c / o)

        per_candle_variance = (
            0.5 * np.square(log_hl)
            - (2.0 * math.log(2.0) - 1.0) * np.square(log_co)
        )
        mean_variance = float(np.mean(per_candle_variance))

        # Do not silently clip a negative estimate.
        if not math.isfinite(mean_variance) or mean_variance < 0:
            raise ValueError(
                f"{self.model_id}: invalid negative variance estimate"
            )

        # Correct annualization: sqrt(variance) * sqrt(periods per year).
        annualized_vol_pct = (
            math.sqrt(mean_variance) * ANNUALIZATION_FACTOR * 100.0
        )

        if not math.isfinite(annualized_vol_pct):
            raise ValueError(
                f"{self.model_id}: calculated volatility is not finite"
            )

        return ModelOutput(
            model_id=self.model_id,
            score=annualized_vol_pct,
            confidence=None,
            feature_vector={"gk_vol": annualized_vol_pct},
        )
