"""
Group E: Correlation Breakdown Detector — PRD §3.5-E. Rolling
correlation between Nifty/BankNifty/FinNifty. A breakdown (correlation
dropping sharply from its usual high level) signals unusual market
structure — sector-specific moves decoupling from the broad index,
which affects both signal reliability and the PRD's correlation-aware
position-sizing cap (§4.8.5).
"""
import math


def _pearson_correlation(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 2 or n != len(y):
        return None
    mean_x, mean_y = sum(x) / n, sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if std_x == 0 or std_y == 0:
        return None
    return cov / (std_x * std_y)


def compute_correlation_breakdown(returns_by_index: dict[str, list[float]],
                                   window: int = 20, low_corr_threshold: float = 0.5) -> dict:
    """
    returns_by_index: {"NIFTY50": [log_returns...], "BANKNIFTY": [...], "FINNIFTY": [...]}
    All lists should be the same length and time-aligned.
    """
    ids = list(returns_by_index.keys())
    if len(ids) < 2:
        return {"pairwise_correlations": {}, "min_correlation": None, "note": "need at least 2 indices"}

    lengths = [len(returns_by_index[i]) for i in ids]
    if min(lengths) < window:
        return {
            "pairwise_correlations": {}, "min_correlation": None,
            "note": f"need at least {window} return samples per index, have {min(lengths)}",
        }

    # Guard against near-flat/closed-market data: if returns are almost
    # all exactly zero (market closed, no real price movement), any
    # correlation computed is spurious/meaningless — a technically-valid
    # 1.0 from near-constant series is misleading, not informative
    # (confirmed via real diagnostic data: BankNifty/FinNifty showed
    # identical repeated closes during after-hours, producing a
    # deceptive 1.0 correlation reading).
    near_flat_indices = []
    for idx_id in ids:
        recent = returns_by_index[idx_id][-window:]
        nonzero_count = sum(1 for r in recent if abs(r) > 1e-9)
        if nonzero_count < window * 0.2:  # fewer than 20% of returns are actually non-zero
            near_flat_indices.append(idx_id)

    if near_flat_indices:
        return {
            "pairwise_correlations": {}, "min_correlation": None, "breakdown": None,
            "note": f"{', '.join(near_flat_indices)} show near-flat/frozen prices in this window "
                    f"(market likely closed or data stale) — correlation would be spurious, not "
                    f"computed. Re-check during active market hours.",
        }

    pairs = {}
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            id_a, id_b = ids[a], ids[b]
            x = returns_by_index[id_a][-window:]
            y = returns_by_index[id_b][-window:]
            corr = _pearson_correlation(x, y)
            pairs[f"{id_a}_{id_b}"] = round(corr, 3) if corr is not None else None

    valid_corrs = [v for v in pairs.values() if v is not None]
    if not valid_corrs:
        return {"pairwise_correlations": pairs, "min_correlation": None, "note": "correlation could not be computed (flat returns)"}

    min_corr = min(valid_corrs)
    breakdown = min_corr < low_corr_threshold

    interpretation = (
        "correlation breakdown detected — indices decoupling, treat as independent bets, not one combined trade"
        if breakdown else
        "indices moving together as usual — combined exposure cap applies (PRD §4.8.5)"
    )

    return {
        "pairwise_correlations": pairs,
        "min_correlation": min_corr,
        "breakdown": breakdown,
        "interpretation": interpretation,
        "note": None,
    }
