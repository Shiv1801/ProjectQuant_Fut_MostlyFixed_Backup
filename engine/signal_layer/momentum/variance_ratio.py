"""
Group C: Autocorrelation / Variance Ratio Test (PRD §3.5-C). Confirms
real statistical persistence vs. pure noise — a second, independent
check on trending/mean-reverting character alongside Hurst.

VR(q) = Var(q-period returns) / (q * Var(1-period returns))
VR > 1 -> persistence/trending (returns are positively autocorrelated)
VR < 1 -> mean-reversion (returns are negatively autocorrelated)
VR = 1 -> consistent with a random walk
"""
MIN_CANDLES_FOR_VR = 50


def compute_variance_ratio(closes: list[float], q: int = 5) -> dict:
    if len(closes) < MIN_CANDLES_FOR_VR:
        return {
            "variance_ratio": None, "persistent": None, "sample_size": len(closes),
            "note": f"need at least {MIN_CANDLES_FOR_VR} candles, have {len(closes)}",
        }

    # 1-period log returns
    log_returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        import math
        log_returns.append(math.log(closes[i] / closes[i - 1]))

    n = len(log_returns)
    if n < q * 4:  # need enough data for q-period aggregation to be meaningful
        return {
            "variance_ratio": None, "persistent": None, "sample_size": len(closes),
            "note": f"insufficient return samples for q={q} aggregation",
        }

    mu = sum(log_returns) / n
    var_1 = sum((r - mu) ** 2 for r in log_returns) / (n - 1)

    if var_1 == 0:
        return {"variance_ratio": 1.0, "persistent": False, "sample_size": len(closes), "note": "flat price series"}

    # q-period returns: non-overlapping sums of q consecutive 1-period returns
    q_returns = []
    for i in range(0, n - q + 1, q):
        q_returns.append(sum(log_returns[i:i + q]))

    if len(q_returns) < 2:
        return {"variance_ratio": None, "persistent": None, "sample_size": len(closes), "note": "not enough q-period windows"}

    mu_q = sum(q_returns) / len(q_returns)
    var_q = sum((r - mu_q) ** 2 for r in q_returns) / (len(q_returns) - 1)

    vr = (var_q / q) / var_1

    persistent = vr > 1.1  # >1 with some margin suggests trending persistence
    mean_reverting = vr < 0.9

    if persistent:
        interpretation = "persistent/trending — momentum signals more reliable"
    elif mean_reverting:
        interpretation = "mean-reverting — reversal signals more reliable"
    else:
        interpretation = "consistent with random walk — no strong statistical edge either way"

    return {
        "variance_ratio": round(vr, 3),
        "persistent": persistent,
        "mean_reverting": mean_reverting,
        "q": q,
        "sample_size": len(closes),
        "interpretation": interpretation,
        "note": None,
    }
