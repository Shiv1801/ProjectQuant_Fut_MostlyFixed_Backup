"""
PRD §4.6, §4.7 — Walk-forward validation. Never let a model see future
data even indirectly. Rolling train/test windows, re-run each time a
parameter or model version is proposed for promotion (§4.7
champion/challenger).
"""
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class WalkForwardWindow:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def generate_walk_forward_windows(full_start: date, full_end: date,
                                   train_months: int = 6, test_months: int = 1) -> list[WalkForwardWindow]:
    windows = []
    cursor = full_start
    while True:
        train_end = cursor + timedelta(days=30 * train_months)
        test_end = train_end + timedelta(days=30 * test_months)
        if test_end > full_end:
            break
        windows.append(WalkForwardWindow(cursor, train_end, train_end, test_end))
        cursor = cursor + timedelta(days=30 * test_months)
    return windows


def validate_model_walk_forward(model_trainer_fn, windows: list[WalkForwardWindow]) -> dict:
    """model_trainer_fn(train_start, train_end) -> trained model; caller then backtests
    that model strictly within [test_start, test_end] and aggregates OOS results."""
    results = []
    for w in windows:
        model = model_trainer_fn(w.train_start, w.train_end)
        oos_result = _backtest_in_window(model, w.test_start, w.test_end)
        results.append(oos_result)
    return {"windows": len(windows), "oos_results": results}


def _backtest_in_window(model, start, end):
    raise NotImplementedError("Delegate to engine.backtest.replay_engine.tick_replay.TickReplayEngine")
