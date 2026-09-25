"""Group B: IV skew (put-call skew slope) — directional sentiment proxy, flags mispriced wings."""
from engine.signal_layer.base_model import QuantModel, ModelOutput


class IvSkew(QuantModel):
    model_id = "iv_skew"
    group = "B"

    def compute(self, market_data: dict) -> ModelOutput:
        otm_put_iv = market_data["otm_put_iv"]
        otm_call_iv = market_data["otm_call_iv"]
        skew = float(otm_put_iv - otm_call_iv)
        return ModelOutput(self.model_id, score=skew, confidence=None, feature_vector={"iv_skew": skew})
