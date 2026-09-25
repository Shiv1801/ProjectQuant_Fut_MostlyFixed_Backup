"""Unit tests for initial hard SL / time-stop logic (PRD §4.8.1)."""
from datetime import datetime, timedelta

from engine.risk_engine.stop_loss import check_initial_stop_loss


def test_hard_sl_triggers_on_premium_drop():
    result = check_initial_stop_loss(
        entry_price=100, current_price=72, entry_ts=datetime.utcnow(), now=datetime.utcnow(),
        premium_drop_pct=27, time_stop_minutes=18, has_moved_favorably=False,
    )
    assert result.triggered is True
    assert result.reason == "hard_sl_hit"


def test_time_stop_triggers_when_stagnant():
    entry_ts = datetime.utcnow() - timedelta(minutes=20)
    result = check_initial_stop_loss(
        entry_price=100, current_price=99, entry_ts=entry_ts, now=datetime.utcnow(),
        premium_drop_pct=27, time_stop_minutes=18, has_moved_favorably=False,
    )
    assert result.triggered is True
    assert result.reason == "time_stop"


def test_no_trigger_when_healthy():
    result = check_initial_stop_loss(
        entry_price=100, current_price=105, entry_ts=datetime.utcnow(), now=datetime.utcnow(),
        premium_drop_pct=27, time_stop_minutes=18, has_moved_favorably=True,
    )
    assert result.triggered is False
