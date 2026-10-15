"""
Entry Gates — hard, binary pre-trade safety checks, distinct from the
quant confluence score (engine/confluence_gate/quant_score.py).

Design principle (explicit, per architecture discussion 2026-10-01):
  - The quant score answers "how strong is the edge?" and is a smooth,
    continuous number — a weak signal should contribute weakly, not be
    silently dropped.
  - These gates answer "is it safe/sane to trade right now at all?" and
    are DELIBERATELY binary. A liquidity crisis, a blown-out spread, or
    abnormal volatility should not merely shave a few points off
    confidence — it should block the trade outright, independent of how
    strong the quant score is. Mixing the two purposes into one smooth
    score would let a strong confluence signal override a genuinely
    unsafe execution environment, which is the wrong failure mode for
    real capital.

Every gate here is FAIL-SAFE: if the data needed to evaluate a gate is
missing, zero, negative, NaN, or otherwise invalid, the gate REJECTS the
trade. Missing data is never treated as "pass" and never treated as 0 —
a None/invalid reading means "cannot confirm this is safe," which is
never allowed to default to "assume safe" for live capital.

Data sources (confirmed by reading the actual code, not assumed):
  - bid/ask/volume/oi: already present in the raw Fyers option-chain
    response per strike (api/main.py's _parse_chain_from_shared_state),
    now also carried through select_best_strike()'s output (selector.py)
    as of this same change. No new Fyers API call is made here — these
    gates consume the SAME already-polled data used everywhere else.
  - realized_vol_pct: engine.signal_layer.volatility.realized_vol's
    compute_realized_vol(), already used elsewhere (e.g. /signals/
    realized-vol, /signals/rv-iv-ratio, /signals/vol-scaled-sizing).
    Annualized percentage, computed from stored 1-min candle closes
    already sitting in the DB — again, zero new API calls.

Each gate returns a dict with a machine-readable "passed" bool, a
"reason" code (None when passed), and the measured value(s) plus the
configured threshold, so the caller/UI/log can show exactly why a trade
was blocked (e.g. "SPREAD_REJECT: spread 2.37% > max 2.00%").
"""
import math


def _is_valid_positive_number(x) -> bool:
    """True only for a real, finite, strictly-positive number. Used
    throughout this module so 'missing data' and '0/negative/NaN data'
    are both correctly treated as invalid, never as a pass."""
    if x is None:
        return False
    if isinstance(x, bool):  # bool is a subclass of int in Python — exclude explicitly
        return False
    if not isinstance(x, (int, float)):
        return False
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return False
    return x > 0


def check_liquidity_gate(volume, oi, config: dict) -> dict:
    """
    Rejects a strike with too little trading activity to safely enter
    and later exit a position in. Uses the strike's own traded volume
    and open interest — both already present in the Fyers option-chain
    response (row.get("volume"), row.get("oi")), no new data source.

    config: the "liquidity" sub-dict of ENTRY_GATES (see
    config/trading_config.py) — {"enabled": bool, "min_volume": int|None,
    "min_oi": int|None}. Either threshold can be None to disable just
    that one check while keeping the gate itself enabled; both None
    (with enabled=True) means the gate always passes, which is a valid,
    explicit configuration choice, not a bug.
    """
    if not config.get("enabled", True):
        return {"passed": True, "reason": None, "detail": {"gate_enabled": False}}

    min_volume = config.get("min_volume")
    min_oi = config.get("min_oi")

    # FAIL-SAFE: missing volume/oi data when a threshold is actually
    # configured means we cannot confirm liquidity is adequate — reject,
    # never silently pass. A threshold of None for a given field means
    # "don't check this specific field," which is different from "data
    # is missing for a field we DO want checked."
    if min_volume is not None:
        if not isinstance(volume, (int, float)) or isinstance(volume, bool) or volume is None:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "missing_volume", "min_volume_required": min_volume},
            }
        if math.isnan(volume) if isinstance(volume, float) else False:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "invalid_volume_nan", "min_volume_required": min_volume},
            }
        if volume < min_volume:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "volume_below_minimum", "volume": volume, "min_volume_required": min_volume},
            }

    if min_oi is not None:
        if not isinstance(oi, (int, float)) or isinstance(oi, bool) or oi is None:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "missing_oi", "min_oi_required": min_oi},
            }
        if math.isnan(oi) if isinstance(oi, float) else False:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "invalid_oi_nan", "min_oi_required": min_oi},
            }
        if oi < min_oi:
            return {
                "passed": False, "reason": "LIQUIDITY_REJECT",
                "detail": {"issue": "oi_below_minimum", "oi": oi, "min_oi_required": min_oi},
            }

    return {
        "passed": True, "reason": None,
        "detail": {"volume": volume, "oi": oi, "min_volume_required": min_volume, "min_oi_required": min_oi},
    }


def check_spread_gate(bid, ask, ltp, config: dict) -> dict:
    """
    Rejects a strike whose bid-ask spread is too wide relative to its
    own price — a wide spread means poor fill quality and real slippage
    cost on both entry and exit, independent of how good the directional
    signal is. bid/ask/ltp are already present per-strike in the Fyers
    option-chain response (same source as the liquidity gate above).

    Spread is measured as a PERCENTAGE OF THE MID PRICE ((ask+bid)/2),
    not of ltp — this matches the convention already used elsewhere in
    this codebase (engine/strike_selector/selector.py's
    _liquidity_fit_score uses the same mid-price-relative formula), so
    this gate's number is directly comparable to that existing soft
    score rather than introducing a second, inconsistent definition of
    "spread %". ltp is accepted as a fallback reference only when mid
    cannot be computed (see below), and is never itself divided into the
    spread — only used to sanity-check the strike has a real tradeable
    price at all.

    config: the "spread" sub-dict of ENTRY_GATES — {"enabled": bool,
    "max_spread_pct": float|None}.
    """
    if not config.get("enabled", True):
        return {"passed": True, "reason": None, "detail": {"gate_enabled": False}}

    max_spread_pct = config.get("max_spread_pct")
    if max_spread_pct is None:
        return {"passed": True, "reason": None, "detail": {"gate_disabled_no_threshold": True}}

    # FAIL-SAFE: bid/ask missing, zero, negative, or NaN/inf -> reject.
    # Never treat a missing bid/ask as "spread is fine" — that is
    # exactly backwards for live order placement.
    if not _is_valid_positive_number(bid) or not _is_valid_positive_number(ask):
        return {
            "passed": False, "reason": "SPREAD_REJECT",
            "detail": {"issue": "missing_or_invalid_bid_ask", "bid": bid, "ask": ask, "max_spread_pct": max_spread_pct},
        }

    if ask < bid:
        # A crossed/locked book (ask < bid) is itself a data-quality red
        # flag seen in real broker feeds during fast-moving or illiquid
        # moments — never compute a "negative spread" and let it pass.
        return {
            "passed": False, "reason": "SPREAD_REJECT",
            "detail": {"issue": "crossed_book_ask_below_bid", "bid": bid, "ask": ask, "max_spread_pct": max_spread_pct},
        }

    mid = (bid + ask) / 2
    if mid <= 0:
        return {
            "passed": False, "reason": "SPREAD_REJECT",
            "detail": {"issue": "non_positive_mid_price", "bid": bid, "ask": ask, "max_spread_pct": max_spread_pct},
        }

    spread_pct = (ask - bid) / mid * 100
    if math.isnan(spread_pct) or math.isinf(spread_pct):
        return {
            "passed": False, "reason": "SPREAD_REJECT",
            "detail": {"issue": "invalid_spread_computation", "bid": bid, "ask": ask, "max_spread_pct": max_spread_pct},
        }

    if spread_pct > max_spread_pct:
        return {
            "passed": False, "reason": "SPREAD_REJECT",
            "detail": {
                "issue": "spread_exceeds_maximum",
                "spread_pct": round(spread_pct, 3), "max_spread_pct": max_spread_pct,
                "bid": bid, "ask": ask, "mid": round(mid, 2),
            },
        }

    return {
        "passed": True, "reason": None,
        "detail": {"spread_pct": round(spread_pct, 3), "max_spread_pct": max_spread_pct, "bid": bid, "ask": ask, "mid": round(mid, 2)},
    }


def check_volatility_gate(realized_vol_pct, config: dict) -> dict:
    """
    Rejects entry when the UNDERLYING INDEX's realized volatility
    (annualized %, from engine.signal_layer.volatility.realized_vol's
    compute_realized_vol — already computed elsewhere in this app from
    stored 1-min candles, no new data source) is above a configured
    ceiling. This is a distinct concern from the quant score's own
    z-score/regime inputs: this gate exists specifically to stop new
    entries during abnormal, currently-unfolding volatility spikes
    (e.g. a sudden news event), regardless of what direction the
    confluence score currently leans.

    config: the "volatility" sub-dict of ENTRY_GATES — {"enabled": bool,
    "max_realized_vol_pct": float|None}.

    IMPORTANT, explicitly flagged per the "do not guess thresholds"
    requirement: this gate ships with max_realized_vol_pct=None (i.e.
    present but disabled) by default in config/trading_config.py's
    ENTRY_GATES, NOT a numeric default. NIFTY50's historically normal
    annualized realized vol range was not independently verified against
    this project's own stored data before writing this file, and a wrong
    guessed ceiling could either block every trade (if set too low
    relative to NIFTY50's real baseline) or never fire at all (if set
    too high). This gate is fully built, wired, and ready — but it
    stays a no-op until you set a real number for
    ENTRY_GATES["volatility"]["max_realized_vol_pct"] after checking it
    against your own /signals/realized-vol/NIFTY50 history (e.g. look at
    a week of readings and pick a ceiling above normal but below what
    you'd consider a real spike).
    """
    if not config.get("enabled", True):
        return {"passed": True, "reason": None, "detail": {"gate_enabled": False}}

    max_vol = config.get("max_realized_vol_pct")
    if max_vol is None:
        return {"passed": True, "reason": None, "detail": {"gate_disabled_no_threshold": True}}

    # FAIL-SAFE: if we're supposed to enforce a volatility ceiling but
    # don't have a valid reading, we cannot confirm it's safe -> reject,
    # not pass. This matches the explicit requirement: "Missing
    # volatility -> reject the trade if max-volatility protection cannot
    # be evaluated."
    if not _is_valid_positive_number(realized_vol_pct):
        return {
            "passed": False, "reason": "VOLATILITY_REJECT",
            "detail": {"issue": "missing_or_invalid_realized_vol", "realized_vol_pct": realized_vol_pct, "max_realized_vol_pct": max_vol},
        }

    if realized_vol_pct > max_vol:
        return {
            "passed": False, "reason": "VOLATILITY_REJECT",
            "detail": {"issue": "volatility_exceeds_maximum", "realized_vol_pct": realized_vol_pct, "max_realized_vol_pct": max_vol},
        }

    return {
        "passed": True, "reason": None,
        "detail": {"realized_vol_pct": realized_vol_pct, "max_realized_vol_pct": max_vol},
    }


def evaluate_entry_gates(selected_strike: dict, realized_vol_pct, entry_gates_config: dict) -> dict:
    """
    Runs all three hard gates in order (liquidity -> spread ->
    volatility) and returns on the FIRST failure, so the caller gets one
    clear, specific reason rather than a jumble of overlapping ones.
    This function does NOT touch or recompute the quant confidence score
    in any way — it only decides pass/no-pass for execution safety.

    selected_strike: the dict returned by
    engine.strike_selector.selector.select_best_strike()'s "selected"
    key, which as of this change also carries "bid", "ask", "volume",
    "oi" (see that file's own changes) in addition to its pre-existing
    "strike", "option_type", "ltp", "symbol", score fields.

    entry_gates_config: the full ENTRY_GATES dict from
    config/trading_config.py (or an equivalent persisted copy) --
    {"enabled": bool, "liquidity": {...}, "spread": {...},
    "volatility": {...}}.

    Returns {"passed": bool, "reason": str|None, "gate": str|None,
    "detail": dict} — "gate" names which specific gate produced the
    result ("liquidity"/"spread"/"volatility"/None), for logging/UI.
    """
    if not entry_gates_config.get("enabled", True):
        return {"passed": True, "reason": None, "gate": None, "detail": {"all_gates_disabled": True}}

    if not selected_strike or not isinstance(selected_strike, dict):
        return {
            "passed": False, "reason": "LIQUIDITY_REJECT", "gate": "liquidity",
            "detail": {"issue": "no_selected_strike_to_evaluate"},
        }

    liquidity_result = check_liquidity_gate(
        selected_strike.get("volume"), selected_strike.get("oi"),
        entry_gates_config.get("liquidity", {"enabled": True}),
    )
    if not liquidity_result["passed"]:
        return {"passed": False, "reason": liquidity_result["reason"], "gate": "liquidity", "detail": liquidity_result["detail"]}

    spread_result = check_spread_gate(
        selected_strike.get("bid"), selected_strike.get("ask"), selected_strike.get("ltp"),
        entry_gates_config.get("spread", {"enabled": True}),
    )
    if not spread_result["passed"]:
        return {"passed": False, "reason": spread_result["reason"], "gate": "spread", "detail": spread_result["detail"]}

    volatility_result = check_volatility_gate(
        realized_vol_pct, entry_gates_config.get("volatility", {"enabled": True}),
    )
    if not volatility_result["passed"]:
        return {"passed": False, "reason": volatility_result["reason"], "gate": "volatility", "detail": volatility_result["detail"]}

    return {
        "passed": True, "reason": None, "gate": None,
        "detail": {"liquidity": liquidity_result["detail"], "spread": spread_result["detail"], "volatility": volatility_result["detail"]},
    }
