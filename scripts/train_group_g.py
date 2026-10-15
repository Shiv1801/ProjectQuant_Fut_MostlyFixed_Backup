"""
Standalone Group G trainer — run this by hand (or on a schedule) whenever
you want to (re)train the Group G GBT classifier on real closed trades.

This does NOT require api/main.py to be modified or uvicorn running -- it
reads directly from your database (read-only until the final save step,
which only writes to engine/signal_layer/ml_ensemble/models/, never to
your trades/positions tables) via config.db.get_model_scores_for_training().

Uses the SAME threshold-gated logic (MIN_TRADES_FOR_GROUP_G) the live app
uses in engine/signal_layer/ml_ensemble/group_g.py -- if you don't have
enough closed, linked trades yet, this will tell you exactly how many more
you need rather than training a garbage model on too little data.

Run from the project root:

    python -m scripts.train_group_g
"""
import sys
import os

sys.path.insert(0, os.getcwd())

from config.db import get_model_scores_for_training, get_model_scores_count
from engine.signal_layer.ml_ensemble.group_g import train_group_g_model, MIN_TRADES_FOR_GROUP_G


def main():
    linked_count = get_model_scores_count(only_linked=True)
    print(f"Closed, linked trades available for Group G training: {linked_count} "
          f"(need at least {MIN_TRADES_FOR_GROUP_G})\n")

    if linked_count < MIN_TRADES_FOR_GROUP_G:
        print(f"Not enough data yet -- Group G stays INACTIVE (score contributes zero weight in "
              f"quant_score.py) until {MIN_TRADES_FOR_GROUP_G} closed trades have accumulated with "
              f"the model_scores logging pipeline running. This is intentional: training a GBT model "
              f"(many parameters) on fewer rows would memorize noise, not learn anything real -- the "
              f"same reasoning as Kelly's 30-trade minimum, scaled up for a model with far more "
              f"capacity to overfit.")
        return

    scored_rows = get_model_scores_for_training(only_linked=True)
    print(f"Loaded {len(scored_rows)} rows, training chronologically (earliest 70% train / "
          f"most recent 30% validate -- never randomly shuffled, this is time-series data)...\n")

    result = train_group_g_model(scored_rows)

    if not result["trained"]:
        print(f"TRAINING SKIPPED: {result['note']}")
        return

    print("=" * 80)
    print("GROUP G TRAINED AND SAVED")
    print("=" * 80)
    print(f"  Trained at:            {result['trained_at']}")
    print(f"  Total trades:          {result['trade_count']}")
    print(f"  Labeled (win/loss):    {result['labeled_count']}")
    print(f"  Train / Validation:    {result['train_count']} / {result['val_count']}")
    print(f"  Train accuracy:        {result['train_accuracy']*100:.1f}%")
    print(f"  Validation accuracy:   {result['val_accuracy']*100:.1f}%")
    print(f"  Baseline (majority):   {result['baseline_accuracy']*100:.1f}%")
    print(f"  Beats baseline:        {result['beats_baseline']}")
    print(f"  Validation Brier score: {result['val_brier_score']}  (lower = better-calibrated, 0=perfect, 0.25=coin-flip-ish)")
    print(f"  Features used:         {len(result['feature_names'])}")
    if result.get("note"):
        print(f"\n  {result['note']}")
    print("\nModel saved to engine/signal_layer/ml_ensemble/models/group_g_gbt.json")
    print("Metadata saved to engine/signal_layer/ml_ensemble/models/group_g_metadata.json")
    print("\nGroup G will now be ACTIVE in the live app (quant_score.py reads this saved model on "
          "every confluence evaluation) as soon as these files are present on the machine running "
          "the app -- no restart-specific action needed beyond having the files there, since "
          "compute_group_g_score() loads them fresh each call.")

    if not result["beats_baseline"]:
        print("\nWARNING: validation accuracy did NOT beat the naive 'always guess the majority "
              "outcome' baseline. The model is saved and will be marked active, but quant_score.py "
              "weights it low specifically because of this flag -- re-run this script again once "
              "more trades have accumulated; a model that doesn't beat baseline on ~150 trades is "
              "common and expected this early, not a bug.")


if __name__ == "__main__":
    main()
