"""
PRD §4.7 — ML Retraining Loop. Batch retrains weekly on a rolling 4-week
window by default (never literally daily on a handful of trades — classic
overfitting trap for low-frequency buying strategies). Walk-forward
validation only. See config/model_registry.yaml for module wiring.
"""
import xgboost as xgb
from loguru import logger


def train_gbt_classifier(X_train, y_train, X_val, y_val, params: dict | None = None) -> xgb.XGBClassifier:
    default_params = {
        "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8, "eval_metric": "logloss",
    }
    params = {**default_params, **(params or {})}
    model = xgb.XGBClassifier(**params)
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    logger.info(f"Trained GBT: val logloss={model.evals_result()['validation_0']['logloss'][-1]:.4f}")
    return model
