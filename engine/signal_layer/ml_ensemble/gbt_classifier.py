"""
Group G: Gradient Boosted Trees (XGBoost/LightGBM) on engineered features
from A-F. Primary signal classifier — outputs probability of a qualifying
25-30%+ move given the current feature vector. Trained/retrained per §4.7.
"""
import xgboost as xgb
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class GbtEnsembleClassifier(QuantModel):
    model_id = "gbt_ensemble_classifier"
    group = "G"

    def __init__(self, model_path: str | None = None):
        self.model = xgb.XGBClassifier()
        if model_path:
            self.model.load_model(model_path)

    def compute(self, market_data: dict) -> ModelOutput:
        feature_vector = market_data["engineered_features"]  # dict of A-F outputs, flattened
        X = np.array([list(feature_vector.values())])
        proba = float(self.model.predict_proba(X)[0, 1])
        return ModelOutput(self.model_id, score=proba, confidence=proba, feature_vector=feature_vector)
