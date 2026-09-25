"""
Group C: Hurst Exponent (rolling window) — PRD §3.5-C. Regime classifier:
gates whether momentum (H>0.5, trending) or mean-reversion (H<0.5)
signals are valid right now. This is "pure math" — a statistical
property of the price series, not a discretionary chart pattern.
"""
import math

MIN_CANDLES_FOR_HURST = 40


def compute_hurst_exponent(closes: list[float], max_lag: int = 20) -> dict:
    """
    Rescaled-range-style Hurst estimate via log-log regression of the
    standard deviation of lagged differences vs lag size.
    H > 0.55 -> trending/persistent
    H < 0.45 -> mean-reverting
    0.45-0.55 -> random walk / no clear regime
    """
    if len(closes) < MIN_CANDLES_FOR_HURST:
        return {
            "hurst": None, "regime": None, "sample_size": len(closes),
            "note": f"need at least {MIN_CANDLES_FOR_HURST} candles, have {len(closes)}",
        }

    n = len(closes)
    max_lag = min(max_lag, n // 2)
    if max_lag < 2:
        return {"hurst": None, "regime": None, "sample_size": n, "note": "not enough candles for any lag"}

    lags = range(2, max_lag)
    tau = []
    valid_lags = []
    for lag in lags:
        diffs = [closes[i + lag] - closes[i] for i in range(n - lag)]
        if len(diffs) < 2:
            continue
        mean_diff = sum(diffs) / len(diffs)
        variance = sum((d - mean_diff) ** 2 for d in diffs) / (len(diffs) - 1)
        std = math.sqrt(variance)
        if std > 0:
            tau.append(std)
            valid_lags.append(lag)

    if len(tau) < 2:
        return {"hurst": None, "regime": None, "sample_size": n, "note": "insufficient variance in price series to estimate Hurst"}

    # Linear regression of log(tau) vs log(lag) -> slope * 2 = Hurst exponent
    log_lags = [math.log(l) for l in valid_lags]
    log_tau = [math.log(t) for t in tau]

    mean_x = sum(log_lags) / len(log_lags)
    mean_y = sum(log_tau) / len(log_tau)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(log_lags, log_tau))
    denominator = sum((x - mean_x) ** 2 for x in log_lags)

    if denominator == 0:
        return {"hurst": None, "regime": None, "sample_size": n, "note": "degenerate regression, cannot estimate Hurst"}

    slope = numerator / denominator
    # slope IS the Hurst exponent directly: std(diff) scales as lag^H,
    # so log(std) = H*log(lag) + const -> slope of that regression = H.
    # (Verified against a known random walk: raw slope came out to ~0.50,
    # matching the theoretical H=0.5 for Brownian motion.)
    hurst = slope

    if hurst > 0.55:
        regime = "trending"
    elif hurst < 0.45:
        regime = "mean_reverting"
    else:
        regime = "random_walk"

    return {
        "hurst": round(hurst, 3),
        "regime": regime,
        "sample_size": n,
        "note": None,
    }
