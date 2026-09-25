"""Group A: Parkinson high-low range estimator — more efficient than close-to-close."""
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class ParkinsonEstimator(QuantModel):
    model_id = "parkinson_estimator"
    group = "A"

    def compute(self, market_data: dict) -> ModelOutput:
        highs, lows = np.array(market_data["high_1min"]), np.array(market_data["low_1min"])
        log_hl2 = np.log(highs / lows) ** 2
        factor = 1.0 / (4.0 * np.log(2))
        vol = float(np.sqrt(factor * np.mean(log_hl2) * 252 * 375))
        return ModelOutput(self.model_id, score=vol, confidence=None, feature_vector={"parkinson_vol": vol})
