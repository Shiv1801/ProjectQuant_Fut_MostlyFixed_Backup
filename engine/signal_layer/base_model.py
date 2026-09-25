"""
Base interface every quant model (§3.5 A-G) implements, so the model
layer / ensemble / backtester can treat them uniformly.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ModelOutput:
    model_id: str
    score: float | None          # standardized signal value, model-specific meaning
    confidence: float | None     # 0-1, used by confluence gate leg 1
    feature_vector: dict         # logged verbatim for ML training / audit (F6, F7)


class QuantModel(ABC):
    model_id: str
    group: str  # A-G

    @abstractmethod
    def compute(self, market_data: dict) -> ModelOutput:
        """market_data contains whatever window of ticks/candles/chain the model needs."""
        raise NotImplementedError
