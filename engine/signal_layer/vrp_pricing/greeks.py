"""
Group B: Black-Scholes / Binomial Greeks engine — delta, gamma, theta, vega,
rho per strike, live. Used for strike selection, theta-bleed monitoring, and
in-trade rejection detection (§4.8.2b needs delta direction in real time).
"""
import math
from scipy.stats import norm

from engine.signal_layer.base_model import QuantModel, ModelOutput


def black_scholes_greeks(S, K, T, r, sigma, option_type="call"):
    """T in years, sigma annualized. Returns dict of greeks."""
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = norm.pdf(d1)

    if option_type == "call":
        delta = norm.cdf(d1)
        theta = (-S * pdf_d1 * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1
        theta = (-S * pdf_d1 * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100

    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T) / 100

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


class GreeksEngine(QuantModel):
    model_id = "greeks_engine"
    group = "B"

    def compute(self, market_data: dict) -> ModelOutput:
        greeks = black_scholes_greeks(
            S=market_data["spot"], K=market_data["strike"], T=market_data["time_to_expiry_years"],
            r=market_data.get("risk_free_rate", 0.065), sigma=market_data["iv"],
            option_type=market_data["opt_type"],
        )
        return ModelOutput(self.model_id, score=greeks["delta"], confidence=None, feature_vector=greeks)
