"""
Group E: Correlation Breakdown Detector.

Calculates pairwise Pearson correlation over the latest `window` aligned return
observations. It is a market-structure diagnostic, not a directional signal.

Important: low correlation alone does not prove that two trades are independent
or that combined exposure should increase. That sizing interpretation must be
validated separately with portfolio-level historical tests.
"""

import math
from itertools import combinations
from numbers import Real


def _to_finite_series(values, label: str) -> list[float]:
    if values is None:
        raise ValueError(f"{label} is missing")
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must contain numeric values") from exc
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{label} contains non-finite values")
    return result


def _pearson_correlation(x: list[float], y: list[float]) -> float | None:
    """Return Pearson r, or None when it is undefined (constant series)."""
    if len(x) < 2 or len(x) != len(y):
        return None

    mean_x = math.fsum(x) / len(x)
    mean_y = math.fsum(y) / len(y)
    centered_x = [value - mean_x for value in x]
    centered_y = [value - mean_y for value in y]
    sum_x2 = math.fsum(value * value for value in centered_x)
    sum_y2 = math.fsum(value * value for value in centered_y)

    if sum_x2 <= 0.0 or sum_y2 <= 0.0:
        return None

    covariance = math.fsum(a * b for a, b in zip(centered_x, centered_y))
    correlation = covariance / math.sqrt(sum_x2 * sum_y2)
    return max(-1.0, min(1.0, correlation))


def compute_correlation_breakdown(
    returns_by_index: dict[str, list[float]],
    window: int = 20,
    low_corr_threshold: float = 0.5,
) -> dict:
    """Calculate pairwise Pearson correlations on the latest aligned returns."""
    empty = {
        "pairwise_correlations": {},
        "min_correlation": None,
        "breakdown": None,
        "sample_count": 0,
        "note": None,
    }

    if not isinstance(returns_by_index, dict) or len(returns_by_index) < 2:
        empty["note"] = "need at least 2 index return series"
        return empty
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        empty["note"] = "window must be an integer >= 2"
        return empty
    if (
        isinstance(low_corr_threshold, bool)
        or not isinstance(low_corr_threshold, Real)
        or not math.isfinite(float(low_corr_threshold))
        or not -1.0 <= float(low_corr_threshold) <= 1.0
    ):
        empty["note"] = "low_corr_threshold must be finite and between -1 and 1"
        return empty

    index_ids = list(returns_by_index)
    try:
        series = {
            index_id: _to_finite_series(returns_by_index[index_id], str(index_id))
            for index_id in index_ids
        }
    except ValueError as exc:
        empty["note"] = str(exc)
        return empty

    lengths = [len(series[index_id]) for index_id in index_ids]
    if len(set(lengths)) != 1:
        empty["note"] = "return series must have equal lengths and be time-aligned"
        return empty

    available = lengths[0]
    if available < window:
        empty["sample_count"] = available
        empty["note"] = f"insufficient observations: need {window}, got {available}"
        return empty

    recent = {index_id: series[index_id][-window:] for index_id in index_ids}
    pairwise = {}
    valid_correlations = []

    for left, right in combinations(index_ids, 2):
        correlation = _pearson_correlation(recent[left], recent[right])
        pairwise[f"{left}|{right}"] = correlation
        if correlation is not None:
            valid_correlations.append(correlation)

    empty["pairwise_correlations"] = pairwise
    empty["sample_count"] = window

    if not valid_correlations:
        empty["note"] = "correlation undefined: all series are constant"
        return empty

    minimum = min(valid_correlations)
    empty["min_correlation"] = minimum
    empty["breakdown"] = minimum < float(low_corr_threshold)
    return empty
