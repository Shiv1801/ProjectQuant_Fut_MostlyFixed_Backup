"""
PRD §4.7 — Champion/challenger framework. New model version must beat
current live model on OUT-OF-SAMPLE data before promotion, not just on
data it was trained on.
"""
from dataclasses import dataclass
from loguru import logger


@dataclass
class PromotionDecision:
    promote: bool
    champion_oos_score: float
    challenger_oos_score: float
    reason: str


def evaluate_promotion(champion_oos_expectancy: float, challenger_oos_expectancy: float,
                        min_improvement_pct: float = 5.0) -> PromotionDecision:
    if champion_oos_expectancy <= 0:
        improvement_pct = float("inf") if challenger_oos_expectancy > 0 else 0
    else:
        improvement_pct = (challenger_oos_expectancy - champion_oos_expectancy) / abs(champion_oos_expectancy) * 100

    promote = improvement_pct >= min_improvement_pct
    reason = f"OOS expectancy improvement {improvement_pct:.1f}% vs {min_improvement_pct}% threshold"
    decision = PromotionDecision(promote, champion_oos_expectancy, challenger_oos_expectancy, reason)
    logger.info(f"Promotion decision: {decision}")
    return decision
