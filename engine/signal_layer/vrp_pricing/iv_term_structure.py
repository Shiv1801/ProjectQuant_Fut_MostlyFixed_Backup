"""Group B: IV term structure (front vs back month/weekly) — expiry selection, avoids front-week IV crush traps."""
from engine.signal_layer.base_model import QuantModel, ModelOutput


class IvTermStructure(QuantModel):
    model_id = "iv_term_structure"
    group = "B"

    def compute(self, market_data: dict) -> ModelOutput:
        front_iv = market_data["front_week_iv"]
        back_iv = market_data["back_month_iv"]
        slope = float(front_iv - back_iv)
        elevated_front = slope > market_data.get("elevated_threshold", 3.0)
        return ModelOutput(
            self.model_id, score=slope, confidence=0.0 if elevated_front else 1.0,
            feature_vector={"front_iv": front_iv, "back_iv": back_iv, "slope": slope, "elevated_front": elevated_front},
        )
