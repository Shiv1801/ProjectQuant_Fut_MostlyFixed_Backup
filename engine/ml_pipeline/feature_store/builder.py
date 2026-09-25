"""
Builds the engineered feature vector (A-F outputs flattened) that feeds
Group G's GBT classifier. One row per model_scores entry (F6 schema),
joined across all group outputs for a given index_id + ts.
"""
def build_feature_vector(model_outputs: dict) -> dict:
    """model_outputs: {model_id: ModelOutput} for all A-F models at this timestamp."""
    flat = {}
    for model_id, output in model_outputs.items():
        if output.score is not None:
            flat[f"{model_id}_score"] = output.score
        for k, v in (output.feature_vector or {}).items():
            if isinstance(v, (int, float, bool)):
                flat[f"{model_id}_{k}"] = v
    return flat
