"""Group C: Tick Momentum Acceleration — rate of change of tick-direction velocity. Entry timing confirmation."""
import numpy as np

from engine.signal_layer.base_model import QuantModel, ModelOutput


class TickMomentumAcceleration(QuantModel):
    model_id = "tick_momentum_acceleration"
    group = "C"

    def compute(self, market_data: dict) -> ModelOutput:
        tick_directions = np.array(market_data["tick_directions"])  # +1/-1/0 per tick
        velocity = np.convolve(tick_directions, np.ones(10) / 10, mode="valid")
        acceleration = float(np.diff(velocity)[-1]) if len(velocity) > 1 else 0.0
        return ModelOutput(self.model_id, score=acceleration, confidence=None, feature_vector={"tick_accel": acceleration})
