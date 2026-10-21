from engine.signal_layer.regime.correlation import compute_correlation_breakdown


def test_correlation_returns_known_value_for_aligned_series():
    result = compute_correlation_breakdown(
        {"NIFTY50": [1, 2, 3, 4], "BANKNIFTY": [2, 4, 6, 8]}, window=4
    )
    assert result["sample_count"] == 4
    assert result["pairwise_correlations"]["NIFTY50|BANKNIFTY"] == 1.0
    assert result["breakdown"] is False


def test_correlation_does_not_turn_invalid_inputs_into_zero():
    insufficient = compute_correlation_breakdown(
        {"NIFTY50": [1, 2], "BANKNIFTY": [2, 4]}, window=3
    )
    constant = compute_correlation_breakdown(
        {"NIFTY50": [1, 1, 1], "BANKNIFTY": [2, 3, 4]}, window=3
    )
    assert insufficient["min_correlation"] is None
    assert "insufficient" in insufficient["note"]
    assert constant["pairwise_correlations"]["NIFTY50|BANKNIFTY"] is None
    assert constant["min_correlation"] is None
