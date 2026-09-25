"""
PRD §4.7 — Decay monitoring. Tracks live performance vs backtested
expectation; auto-flags and de-risks if live diverges beyond a
control-chart threshold (regime change signal). Also feature drift
monitoring: alert if input feature distributions shift materially.
"""
import numpy as np
from loguru import logger


def check_performance_decay(live_expectancy_rolling: list[float], backtest_expectancy: float,
                             control_chart_sigma: float = 2.0) -> dict:
    live_mean = np.mean(live_expectancy_rolling)
    live_std = np.std(live_expectancy_rolling) or 1e-6
    z = (live_mean - backtest_expectancy) / live_std
    decayed = abs(z) > control_chart_sigma and live_mean < backtest_expectancy
    if decayed:
        logger.warning(f"Model decay detected: live_mean={live_mean:.3f} vs backtest={backtest_expectancy:.3f}, z={z:.2f}")
    return {"decayed": decayed, "z_score": float(z), "live_mean": float(live_mean)}


def check_feature_drift(live_feature_distribution: np.ndarray, training_feature_distribution: np.ndarray,
                         threshold: float = 0.15) -> dict:
    """Simple population stability index (PSI)-style check."""
    live_hist, edges = np.histogram(live_feature_distribution, bins=10, density=True)
    train_hist, _ = np.histogram(training_feature_distribution, bins=edges, density=True)
    live_hist = np.clip(live_hist, 1e-6, None)
    train_hist = np.clip(train_hist, 1e-6, None)
    psi = float(np.sum((live_hist - train_hist) * np.log(live_hist / train_hist)))
    return {"drifted": psi > threshold, "psi": psi}
