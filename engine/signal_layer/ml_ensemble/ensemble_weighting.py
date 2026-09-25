"""
Group G: Walk-forward validated ensemble weighting — rolling live-expectancy
weighted combination of Model A-D outputs. Reweighted as models prove out
or decay (feeds into engine/ml_pipeline/decay_monitor).
"""
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class WalkForwardEnsembleWeighting(QuantModel):
    model_id = "walk_forward_ensemble_weighting"
    group = "G"

    def compute(self, market_data: dict) -> ModelOutput:
        model_scores = market_data["component_scores"]     # {model_id: score}
        model_weights = market_data["rolling_expectancy_weights"]  # {model_id: weight}, sums to 1
        weighted = sum(model_scores[m] * model_weights.get(m, 0) for m in model_scores if model_scores[m] is not None)
        return ModelOutput(self.model_id, score=float(weighted), confidence=None, feature_vector={"weighted_score": float(weighted)})
