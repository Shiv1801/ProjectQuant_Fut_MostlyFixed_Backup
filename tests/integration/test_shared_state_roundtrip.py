"""
Integration test: publish via engine.shared_state.publisher, read back via
subscriber, confirming the §7.3b single-write-path contract holds end to end.
Requires a running Redis instance (see deploy/docker/docker-compose.yml).
"""
import pytest

from engine.shared_state.publisher import publish_model_score
from engine.shared_state.subscriber import get_latest


@pytest.mark.skip(reason="Requires live Redis — run against docker-compose stack")
def test_model_score_roundtrip():
    publish_model_score("NIFTY50", "hurst_exponent", {"score": 0.62})
    result = get_latest("latest:model_score:NIFTY50:hurst_exponent")
    assert result["score"] == 0.62
