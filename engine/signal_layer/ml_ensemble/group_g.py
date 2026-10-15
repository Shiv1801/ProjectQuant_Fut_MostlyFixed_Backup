"""
Group G: GBT (XGBoost) directional classifier — threshold-gated activation,
added 2026-09-30 per explicit request: "set a minimum threshold of x trades
data required to activate the model like you did for kelly model."

SCOPE, STATED PLAINLY (read before assuming this does more than it does):
Group G, as wired here, contributes ONE additional vote into
compute_quant_confidence() (Leg 1 of the confluence gate) — a probability
that the CURRENT feature snapshot (Groups A-F outputs at this instant)
precedes a favorable move, learned from real closed trades. It does NOT:
  - change entry TIMING (that's auto_entry_loop's polling cadence)
  - change STRIKE selection (that's engine/strike_selector/selector.py)
  - directly assess IV vs realized vol as a standalone gate (VRP/IV Rank,
    Group B, already do that — Group G can only learn to weight that
    existing signal more/less if the data shows it matters)
  - change EXIT management (that's engine/risk_engine/stops.py — SL/TSL/
    time-stop logic is unrelated to this file)
A well-trained Group G can indirectly improve some of these by making Leg 1
more selective (fewer bad setups fire at all), but it does not touch those
subsystems directly. If entry timing / strike selection / exit management
need to improve, those are separate, later pieces of work in their own
files — flagged, not silently promised here.

THRESHOLD RATIONALE (why higher than Kelly's 30):
Kelly's MIN_TRADES_FOR_KELLY=30 estimates 2 numbers (win_rate, payoff
ratio) — a GBT classifier with dozens of engineered features has vastly
more capacity to overfit a small sample (it can memorize 30-100 rows
perfectly and generalize to nothing). MIN_TRADES_FOR_GROUP_G=150 closed,
linked trades is the floor used here, split chronologically 70/30 into
train/validation (never shuffled — this is time-series data, and random
shuffling would leak future information into training, inflating
validation accuracy in a way that would not hold up live). Below the
threshold, compute_group_g_score() returns the exact same
None-with-explanatory-note shape compute_fractional_kelly() does below
its own threshold, and the model votes ZERO weight in quant_score.py —
inactive, not guessing.
"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np

MIN_TRADES_FOR_GROUP_G = 150
VALIDATION_FRACTION = 0.30  # last 30% of chronologically-ordered trades, held out
IST = timezone(timedelta(hours=5, minutes=30))

MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "group_g_gbt.json"          # xgboost's own save_model format
METADATA_PATH = MODEL_DIR / "group_g_metadata.json"  # feature order, trained_at, metrics, trade_count


def _label_from_pnl_pct(pnl_pct: float | None) -> int | None:
    """Binary label: 1 = winning trade, 0 = losing trade. Breakeven (exactly
    0) and missing pnl_pct are dropped (None), not forced into either class
    — a forced label on ambiguous outcomes teaches the model noise."""
    if pnl_pct is None:
        return None
    if pnl_pct > 0:
        return 1
    if pnl_pct < 0:
        return 0
    return None


def build_training_matrix(scored_rows: list[dict]) -> tuple[list[str], np.ndarray, np.ndarray, list[dict]]:
    """
    scored_rows: output of config.db.get_model_scores_for_training(only_linked=True)
    — each row already JOINed with its resulting trade's pnl_pct.

    Returns (feature_names, X, y, kept_rows) in the SAME chronological order
    the rows arrived in (get_model_scores_for_training orders by
    ms.timestamp ASC already) — caller does the chronological split, this
    function must not reorder or shuffle anything.

    Feature set: the UNION of every scalar key seen across all rows'
    feature_vector (Groups A-F flattened, see api/main.py's
    _flatten_model_outputs_for_logging), since different rows may be
    missing different fields (e.g. option chain not ready yet at that
    moment). Missing values for a row are filled with 0.0 (a neutral
    fill for these zscore/ofi/pcr-style features, mirroring the "no
    vote" = 0 convention already used throughout quant_score.py's
    _vote_from_* functions), and a companion "<field>_present" indicator
    column is added so the model itself can distinguish "0.0 measured"
    from "field wasn't available yet" rather than conflating the two.
    """
    all_keys: set[str] = set()
    for row in scored_rows:
        all_keys.update(row["feature_vector"].keys())
    feature_names = sorted(all_keys)

    X_rows = []
    y_rows = []
    kept_rows = []
    for row in scored_rows:
        label = _label_from_pnl_pct(row.get("pnl_pct"))
        if label is None:
            continue
        fv = row["feature_vector"]
        vec = []
        for k in feature_names:
            v = fv.get(k)
            vec.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (1.0 if v is True else 0.0))
        # presence indicators, appended after the raw values, same order
        vec.extend([1.0 if k in fv and fv[k] is not None else 0.0 for k in feature_names])
        X_rows.append(vec)
        y_rows.append(label)
        kept_rows.append(row)

    presence_names = [f"{k}__present" for k in feature_names]
    full_feature_names = feature_names + presence_names
    X = np.array(X_rows, dtype=float) if X_rows else np.zeros((0, len(full_feature_names)))
    y = np.array(y_rows, dtype=int) if y_rows else np.zeros((0,), dtype=int)
    return full_feature_names, X, y, kept_rows


def train_group_g_model(scored_rows: list[dict]) -> dict:
    """
    Trains a fresh XGBoost classifier on scored_rows (already filtered to
    only_linked=True, closed trades) and persists it to MODEL_PATH +
    METADATA_PATH. Returns a result dict describing what happened —
    including an explicit refusal if trade_count is still below
    MIN_TRADES_FOR_GROUP_G (caller should check compute_group_g_score()'s
    own gate before calling this, but this function re-checks
    independently so it's never possible to train a garbage model by
    calling this directly and skipping the gate).

    CHRONOLOGICAL split (not sklearn's default shuffled train_test_split):
    the earliest ~70% of trades train the model, the most recent ~30%
    validate it — the same walk-forward principle already used in
    engine/backtest sweeps. This is the only honest way to estimate how
    the model will perform on trades it hasn't seen yet, since a
    randomly-shuffled split would let the model "see the future"
    relative to some of its own training rows.
    """
    trade_count = len(scored_rows)
    if trade_count < MIN_TRADES_FOR_GROUP_G:
        return {
            "trained": False,
            "trade_count": trade_count,
            "note": f"need at least {MIN_TRADES_FOR_GROUP_G} closed, linked trades to train Group G "
                    f"without overfitting, have {trade_count}",
        }

    feature_names, X, y, kept_rows = build_training_matrix(scored_rows)
    if len(y) < MIN_TRADES_FOR_GROUP_G:
        # scored_rows had enough rows, but too many lacked a usable pnl_pct
        # label (breakeven/missing) to actually build MIN_TRADES_FOR_GROUP_G
        # labeled examples -- same refusal, different reason, stated honestly.
        return {
            "trained": False,
            "trade_count": trade_count,
            "labeled_count": len(y),
            "note": f"only {len(y)} of {trade_count} linked trades had a usable win/loss label "
                    f"(non-zero pnl_pct) -- need at least {MIN_TRADES_FOR_GROUP_G} labeled rows",
        }

    if len(set(y.tolist())) < 2:
        return {
            "trained": False,
            "trade_count": trade_count,
            "note": "all labeled trades are the same outcome (all wins or all losses) -- "
                    "cannot train a classifier with only one class present",
        }

    split_idx = int(len(y) * (1 - VALIDATION_FRACTION))
    split_idx = max(1, min(split_idx, len(y) - 1))  # keep both sides non-empty
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    if len(set(y_train.tolist())) < 2:
        return {
            "trained": False,
            "trade_count": trade_count,
            "note": "training split ended up with only one outcome class present (data is heavily "
                    "streaky/ordered) -- cannot train; needs more varied trade history",
        }

    import xgboost as xgb
    model = xgb.XGBClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=1.0,  # L1/L2 regularization -- explicit anti-overfitting per the request
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    val_pred = model.predict(X_val)
    val_proba = model.predict_proba(X_val)[:, 1]
    val_accuracy = float(np.mean(val_pred == y_val))
    # Brier score: calibration quality of the predicted probabilities, not
    # just accuracy -- a confidence score that's badly calibrated (e.g.
    # always predicts 0.9 regardless of outcome) is worse than useless for
    # a vote that's meant to be weighted by confidence.
    val_brier = float(np.mean((val_proba - y_val) ** 2))
    train_accuracy = float(np.mean(model.predict(X_train) == y_train))

    baseline_accuracy = float(max(np.mean(y_val), 1 - np.mean(y_val)))  # "always predict majority class"
    beats_baseline = val_accuracy > baseline_accuracy

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save_model(str(MODEL_PATH))

    metadata = {
        "trained_at": datetime.now(IST).isoformat(),
        "feature_names": feature_names,
        "trade_count": trade_count,
        "labeled_count": len(y),
        "train_count": int(len(y_train)),
        "val_count": int(len(y_val)),
        "train_accuracy": round(train_accuracy, 4),
        "val_accuracy": round(val_accuracy, 4),
        "val_brier_score": round(val_brier, 4),
        "baseline_accuracy": round(baseline_accuracy, 4),
        "beats_baseline": beats_baseline,
        "min_trades_threshold": MIN_TRADES_FOR_GROUP_G,
    }
    METADATA_PATH.write_text(json.dumps(metadata, indent=2))

    note = None
    if not beats_baseline:
        note = ("WARNING: validation accuracy does not beat the naive 'always predict the majority "
                "outcome' baseline -- the model was saved (so it's inspectable) but should be treated "
                "as not yet useful; compute_group_g_score() will still gate on sample size, not on "
                "this quality check, so a human should review val_accuracy vs baseline_accuracy before "
                "trusting its vote")

    return {"trained": True, **metadata, "note": note}


def _load_model():
    """Returns (model, metadata) or (None, None) if no trained model is persisted yet."""
    if not MODEL_PATH.exists() or not METADATA_PATH.exists():
        return None, None
    try:
        import xgboost as xgb
        model = xgb.XGBClassifier()
        model.load_model(str(MODEL_PATH))
        metadata = json.loads(METADATA_PATH.read_text())
        return model, metadata
    except Exception:
        return None, None


def compute_group_g_score(feature_vector: dict, live_trade_count: int) -> dict:
    """
    Threshold-gated Group G score — the function quant_score.py and the
    /signals/group-g/{index_id} endpoint both call. Mirrors
    compute_fractional_kelly()'s exact contract: returns a
    None-with-explanatory-note shape when inactive (below threshold, or no
    trained model persisted yet), never a fabricated score.

    live_trade_count: the CURRENT count of closed, linked trades (from
    config.db.get_model_scores_count(only_linked=True)) — the caller passes
    this in fresh each call rather than this function querying the DB
    itself, keeping it a pure function like compute_fractional_kelly().
    """
    if live_trade_count < MIN_TRADES_FOR_GROUP_G:
        return {
            "active": False, "score": None, "confidence": None,
            "trade_count": live_trade_count,
            "note": f"need at least {MIN_TRADES_FOR_GROUP_G} closed, linked trades to activate Group G, "
                    f"have {live_trade_count}",
        }

    model, metadata = _load_model()
    if model is None:
        return {
            "active": False, "score": None, "confidence": None,
            "trade_count": live_trade_count,
            "note": f"{live_trade_count} trades available (threshold met) but no trained model is "
                    f"persisted yet -- run the training script (engine/ml_pipeline/train_group_g.py) "
                    f"to train and activate it",
        }

    feature_names = metadata["feature_names"]
    # feature_names includes the "__present" indicator columns appended by
    # build_training_matrix -- reconstruct the same vector shape at
    # inference time so column order matches exactly what the model was
    # trained on.
    raw_names = [n for n in feature_names if not n.endswith("__present")]
    vec = []
    for k in raw_names:
        v = feature_vector.get(k)
        vec.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (1.0 if v is True else 0.0))
    vec.extend([1.0 if k in feature_vector and feature_vector[k] is not None else 0.0 for k in raw_names])

    X = np.array([vec], dtype=float)
    proba = float(model.predict_proba(X)[0, 1])  # P(winning trade | current feature snapshot)

    return {
        "active": True,
        "score": round(proba, 4),
        "confidence": round(abs(proba - 0.5) * 2, 4),  # 0 at proba=0.5 (no edge), 1 at proba=0 or 1 (max edge)
        "trade_count": live_trade_count,
        "model_val_accuracy": metadata.get("val_accuracy"),
        "model_beats_baseline": metadata.get("beats_baseline"),
        "model_trained_at": metadata.get("trained_at"),
        "note": None if metadata.get("beats_baseline") else
                "model is active (threshold met) but its own validation accuracy did not beat the "
                "naive majority-class baseline at training time -- weighted low in quant_score.py for "
                "this reason, not excluded, so it can still contribute once retrained on more data",
    }
