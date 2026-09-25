"""
Weekly-batch retrain trigger (PRD §4.7 recommendation — literal daily
retraining on low trade counts is an overfitting trap). Schedule via
systemd timer / APScheduler for a weekly cadence.
"""
from engine.ml_pipeline.training.train_gbt import train_gbt_classifier
from engine.ml_pipeline.champion_challenger.promoter import evaluate_promotion
from engine.utils.logging_setup import setup_logging

logger = setup_logging()


def main():
    logger.info("Weekly retrain triggered")
    raise NotImplementedError(
        "1. Pull rolling 4-week feature/label window from feature_store\n"
        "2. Walk-forward validate (engine.backtest.walk_forward.validator)\n"
        "3. Train challenger via train_gbt_classifier\n"
        "4. evaluate_promotion(champion_oos, challenger_oos) before swapping model_versions.promoted"
    )


if __name__ == "__main__":
    main()
