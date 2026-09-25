"""Group A: Garman-Klass OHLC estimator — captures overnight/intraday gaps."""
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class GarmanKlassEstimator(QuantModel):
    model_id = "garman_klass_estimator"
    group = "A"

    def compute(self, market_data: dict) -> ModelOutput:
        o = np.array(market_data["open_1min"])
        h = np.array(market_data["high_1min"])
        l = np.array(market_data["low_1min"])
        c = np.array(market_data["close_1min"])
        term1 = 0.5 * (np.log(h / l)) ** 2
        term2 = (2 * np.log(2) - 1) * (np.log(c / o)) ** 2
        vol = float(np.sqrt(np.mean(term1 - term2) * 252 * 375))
        return ModelOutput(self.model_id, score=vol, confidence=None, feature_vector={"gk_vol": vol})
