"""
Group F: Volatility-Scaled Position Sizing — PRD §3.5-F. Adjusts size
inversely to current volatility, keeping risk-per-trade roughly
constant across calm and volatile regimes. Complements Kelly (which
sizes off edge) by additionally sizing off current risk conditions.
"""
MIN_SCALE = 0.25   # floor: never scale below 1/4 of base size
MAX_SCALE = 1.5    # ceiling: never scale above 1.5x base size


def compute_vol_scaled_size(base_position_size: float | None, current_vol_pct: float | None,
                             reference_vol_pct: float | None) -> dict:
    if base_position_size is None:
        return {"adjusted_size": None, "scale": None, "note": "missing base position size"}

    if current_vol_pct is None or reference_vol_pct is None:
        return {"adjusted_size": base_position_size, "scale": 1.0, "note": "missing vol inputs — using base size unscaled"}

    if current_vol_pct <= 0:
        return {"adjusted_size": base_position_size, "scale": 1.0, "note": "current vol is zero/invalid — using base size unscaled"}

    raw_scale = reference_vol_pct / current_vol_pct
    scale = max(MIN_SCALE, min(MAX_SCALE, raw_scale))
    adjusted_size = base_position_size * scale

    if scale == MIN_SCALE:
        note = f"scale clamped at floor ({MIN_SCALE}x) — current vol far above reference"
    elif scale == MAX_SCALE:
        note = f"scale clamped at ceiling ({MAX_SCALE}x) — current vol far below reference"
    else:
        note = None

    return {
        "adjusted_size": round(adjusted_size, 2),
        "scale": round(scale, 3),
        "raw_scale_before_clamp": round(raw_scale, 3),
        "current_vol_pct": current_vol_pct,
        "reference_vol_pct": reference_vol_pct,
        "note": note,
    }
