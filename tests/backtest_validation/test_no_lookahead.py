"""
PRD §7.4 gap — "Backtest survivorship/lookahead bias audit: explicit
checklist step to confirm the backtest engine can't peek at data not yet
available at each simulated timestamp." This test file is the audit.
"""
import pytest


@pytest.mark.skip(reason="Implement once TickReplayEngine._merged_chronological_stream is built")
def test_model_never_sees_future_tick():
    """At simulated ts=T, confirm no tick/option_chain row with ts > T is ever
    passed into a model's market_data argument."""
    pass


@pytest.mark.skip(reason="Implement once walk-forward validator is complete")
def test_walk_forward_train_test_no_overlap():
    """Confirm train_end <= test_start for every generated WalkForwardWindow."""
    pass
