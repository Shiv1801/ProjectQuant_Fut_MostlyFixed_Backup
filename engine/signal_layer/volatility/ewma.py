"""Group A: EWMA volatility (RiskMetrics-style) — faster-reacting than rolling window."""
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class EwmaVolatility(QuantModel):
    model_id = "ewma_vol"
    group = "A"
    lam = 0.94  # RiskMetrics standard decay factor

    def compute(self, market_data: dict) -> ModelOutput:
        closes = np.array(market_data["close_1min"])
        log_ret = np.diff(np.log(closes))
        var = log_ret[0] ** 2
        for r in log_ret[1:]:
            var = self.lam * var + (1 - self.lam) * r ** 2
        vol = float(np.sqrt(var * 252 * 375))
        return ModelOutput(self.model_id, score=vol, confidence=None, feature_vector={"ewma_vol": vol})
