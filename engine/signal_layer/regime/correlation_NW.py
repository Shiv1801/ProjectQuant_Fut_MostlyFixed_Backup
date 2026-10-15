"""
Group E: Correlation Breakdown Detector.

Calculates pairwise Pearson correlation over the latest `window` aligned return
observations. It is a market-structure diagnostic, not a directional signal.

Important: low correlation alone does not prove that two trades are independent
or that combined exposure should increase. That sizing interpretation must be
validated separately with portfolio-level historical tests.
"""
import math
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
    # Clamp only floating-point roundoff beyond the mathematical [-1, 1] range.
    return max(-1.0, min(1.0, correlation))


def compute_correlation_breakdown(
    returns_by_index: dict[str, list[float]],
    window: int = 20,
    low_corr_threshold: float = 0.5,
) -> dict:
    """
    Args:
        returns_by_index: mapping index name -> time-aligned returns.
        window: number of latest return observations used for each pair.
        low_corr_threshold: breakdown is flagged when the minimum valid pair
            correlation is strictly below this threshold.

    Returns the established keys, adding sample_count for auditability.
    """
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