"""
Confluence Gate — Leg 2: Chart Structure Confirmation (PRD §4.3.5).
Statistically-defined check on recent 1-min candles, NOT discretionary
pattern-reading. Confirms whether recent price action actually supports
the direction Leg 1 (quant score) is proposing, before the gate fires.
"""


def confirm_chart_structure(candles: list[dict], expected_direction: str,
                             confirmation_candles: int = 2) -> dict:
    """
    candles: recent 1-min candles (oldest first), each with open/high/low/close.
    expected_direction: "long_call" (bullish) or "long_put" (bearish).

    Confirms using simple, objective structure: has price made a higher
    low (bullish) / lower high (bearish) in the last few candles, or
    broken the recent swing high/low in the expected direction.
    """
    if not candles or len(candles) < 10:
        return {"verdict": "insufficient_data", "confirmed": False, "note": f"need at least 10 candles, have {len(candles)}"}

    if expected_direction not in ("long_call", "long_put"):
        return {"verdict": "invalid_direction", "confirmed": False, "note": f"unexpected direction: {expected_direction}"}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    recent_highs = highs[-confirmation_candles:]
    recent_lows = lows[-confirmation_candles:]
    swing_highs = highs[-10:-confirmation_candles] if len(highs) > 10 else highs[:-confirmation_candles]
    swing_lows = lows[-10:-confirmation_candles] if len(lows) > 10 else lows[:-confirmation_candles]

    if not swing_highs or not swing_lows:
        return {"verdict": "insufficient_data", "confirmed": False, "note": "not enough candles for swing reference"}

    swing_high = max(swing_highs)
    swing_low = min(swing_lows)

    if expected_direction == "long_call":
        higher_low = lows[-1] > lows[-3] if len(lows) >= 3 else False
        broke_swing_high = closes[-1] > swing_high
        confirmed = higher_low or broke_swing_high
        opposite = closes[-1] < swing_low
        detail = {"higher_low": higher_low, "broke_swing_high": broke_swing_high, "swing_high": swing_high, "swing_low": swing_low}
    else:  # long_put
        lower_high = highs[-1] < highs[-3] if len(highs) >= 3 else False
        broke_swing_low = closes[-1] < swing_low
        confirmed = lower_high or broke_swing_low
        opposite = closes[-1] > swing_high
        detail = {"lower_high": lower_high, "broke_swing_low": broke_swing_low, "swing_high": swing_high, "swing_low": swing_low}

    if opposite:
        verdict = "opposite"
        confirmed = False
    elif confirmed:
        verdict = "confirms"
    else:
        verdict = "no_signal"

    return {"verdict": verdict, "confirmed": confirmed, **detail, "note": None}
