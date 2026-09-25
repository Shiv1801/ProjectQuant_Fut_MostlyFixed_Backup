"""Shared pytest fixtures."""
import pytest


@pytest.fixture
def sample_option_chain_row():
    return {
        "index_id": "NIFTY50", "expiry": "24JAN", "strike": 25000, "opt_type": "CE",
        "iv": 14.5, "delta": 0.35, "gamma": 0.002, "theta": -8.2, "vega": 12.1, "oi": 150000,
    }
