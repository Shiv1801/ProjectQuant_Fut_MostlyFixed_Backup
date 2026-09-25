"""
Model F: Regime Chop Classifier (PRD §9.1 roadmap suggestion, status=roadmap
in config/model_registry.yaml). Predicts choppy/low-edge regimes explicitly
rather than inferring from a low confidence score on the main model — chop
detection is a distinct pattern, not just "not confident".

NOT wired into the confluence gate by default (still two-leg AND per PRD
§4.3.5) until this model is independently backtested and validated per
§4.7. Flip config/confluence_gate.enable_chop_veto = true only after that.
"""
from engine.signal_layer.base_model import QuantModel, ModelOutput


class RegimeChopClassifier(QuantModel):
    model_id = "regime_chop_classifier"
    group = "F"
    status = "roadmap"  # not yet backtested/promoted — see docstring

    def compute(self, market_data: dict) -> ModelOutput:
        raise NotImplementedError(
            "Model F is a PRD roadmap item (§9.1) — implement + backtest before use. "
            "Suggested features: Hurst near 0.5, low realized vol, tight Bollinger-style "
            "range on 1-min closes, low OFI magnitude, elevated whipsaw count."
        )
