"""
Per-index risk & trading configuration. Each index (NIFTY50, BANKNIFTY,
FINNIFTY) has its OWN full settings: risk params, trading mode
(paper/live/both/off), and its own share of total capital. This
replaces the earlier single global RISK_CONFIG — different indices can
now genuinely need different settings, matched by the annual sweep
findings (Nifty and FinNifty had good, trustworthy settings; BankNifty
did not and should stay off until revisited).

DEFAULT_* dicts are fixed, never-mutated baselines that reset restores
from — same guarantee as before, now per-index.

PERSISTENCE: INDEX_CONFIG and ACCOUNT_CONFIG are loaded from the
database (app_settings table) on module import, and every mutating
function saves back to the database immediately after changing the
in-memory value. This fixes a real, confirmed bug — previously these
were pure in-memory dicts that silently reset to hardcoded defaults on
every server restart, making it look like settings "wouldn't save"
even though the Save button and API were both working correctly; the
problem was that nothing was ever being persisted anywhere durable.
"""
from config.db import save_setting, load_setting

INDICES = ["NIFTY50"]  # BankNifty and FinNifty removed per explicit request —
# this app now trades NIFTY50 only. Capacity previously split three
# ways (data polling, capital allocation) is now dedicated entirely to
# Nifty.

DEFAULT_RISK_PARAMS = {
    "hard_sl_pct": 9.0,
    "time_stop_minutes": 7,
    "tsl_activation_pct": 5.0,
    "tsl_base_trail_pct": 3.0,
    "tsl_k": 0.15,
    "tsl_floor_pct": 5.0,
    "risk_per_trade_pct": 1.0,
    "min_confluence_confidence": 0.95,
    "max_entry_iv_rank": None,
    # Leg 3 of the confluence gate (VWAP). Defaults ON per explicit
    # request. IMPORTANT CONTEXT PRESERVED HERE: this signal has only
    # ever been tested in a backtest later found to carry multiple
    # serious, confirmed bugs (a septillion-percent math error, an
    # outlier-trade concentration issue, and an overnight-volatility
    # spike that overpriced modeled premiums by 70%+ against a real
    # trade) — it has NEVER been validated in live paper trading. This
    # default was changed from OFF to ON on explicit instruction,
    # against this assistant's recommendation; that disagreement is
    # recorded, not silently overridden, and the toggle remains
    # available to turn back off from Positions & Risk at any time.
    "require_vwap": True,
}

# Per-index defaults — same PRD baseline for all three initially; each
# can be independently tuned/applied from backtest results going forward.
DEFAULT_INDEX_CONFIG = {
    index_id: {
        "risk_params": dict(DEFAULT_RISK_PARAMS),
        "mode": "paper",  # "paper" | "live" | "both" | "off"
        "capital_allocation_pct": round(100.0 / len(INDICES), 4),  # 100% now that Nifty is the only index
    }
    for index_id in INDICES
}

# Live, tunable copy — starts as a deep copy of defaults, THEN
# overwritten by whatever was last persisted to the database, if
# anything. This means a fresh install behaves exactly as before
# (PRD defaults), but any settings you've actually saved survive a
# restart from here on.
INDEX_CONFIG = {
    index_id: {
        "risk_params": dict(cfg["risk_params"]),
        "mode": cfg["mode"],
        "capital_allocation_pct": cfg["capital_allocation_pct"],
    }
    for index_id, cfg in DEFAULT_INDEX_CONFIG.items()
}

_persisted_index_config = load_setting("index_config")
if _persisted_index_config:
    for _index_id, _cfg in _persisted_index_config.items():
        if _index_id in INDEX_CONFIG:
            INDEX_CONFIG[_index_id] = _cfg

# Account-level settings that aren't per-index (total capital pool,
# shared circuit breakers, auto-entry polling cadence).
#
# auto_entry_enabled now defaults to True (per explicit request) —
# previously defaulted to False as an initial safety measure while the
# system was still being built/verified; now that the app has real
# per-index Off/Paper/Live/Both controls as the actual safety
# mechanism, auto-entry itself should stay on by default and only stop
# when a person explicitly turns it off, rather than silently reverting
# to off on every restart.
DEFAULT_ACCOUNT_CONFIG = {
    "total_available_capital": 150000.0,
    "auto_entry_enabled": True,
    "auto_entry_poll_seconds": 10,
    "daily_loss_limit_pct": 5.0,
    "consecutive_loss_throttle": 3,
    "consecutive_loss_size_multiplier": 0.5,
    "max_concurrent_positions": 3,
    "weekly_drawdown_cap_pct": 15.0,
    # Strikes tracked EACH SIDE of ATM in the option chain poller (10
    # default -> 21 total strikes: 10 + 10 + ATM itself). Directly
    # controls the scope used by the new "strike_out_of_scope" exit
    # rule — a position whose strike falls outside this tracked range
    # is force-closed. Configurable here so it can be tuned from
    # Settings without a code change.
    "strikes_tracked_per_side": 10,
    # Real Fyers account balance, refreshed periodically from the live
    # funds API (see engine/data_layer/fyers_client/rest_client.py's
    # get_funds and engine/background/pollers.py's account_balance_poller).
    # None until the first successful real fetch.
    "real_available_balance": None,
    "real_balance_last_updated": None,
    # Master safety switch for REAL order placement, separate from and
    # in addition to each index's own mode setting. Both this AND the
    # index's mode must allow it for a real order to ever be placed --
    # defense in depth, given the seriousness of real capital risk. See
    # engine/execution/trade_dispatcher.py's is_live_trading_active_for.
    "live_trading_master_enabled": False,
}
ACCOUNT_CONFIG = dict(DEFAULT_ACCOUNT_CONFIG)

_persisted_account_config = load_setting("account_config")
if _persisted_account_config:
    ACCOUNT_CONFIG.update(_persisted_account_config)
# Ensure any NEW keys added to DEFAULT_ACCOUNT_CONFIG since a person's
# last save (e.g. the live-trading fields above) are present even for
# an account_config persisted before those keys existed -- otherwise a
# pre-existing saved config could be missing keys other code assumes
# are always there.
for _k, _v in DEFAULT_ACCOUNT_CONFIG.items():
    if _k not in ACCOUNT_CONFIG:
        ACCOUNT_CONFIG[_k] = _v


def _persist_index_config():
    save_setting("index_config", INDEX_CONFIG)


def _persist_account_config():
    save_setting("account_config", ACCOUNT_CONFIG)


def get_index_config(index_id: str) -> dict | None:
    cfg = INDEX_CONFIG.get(index_id.upper())
    if cfg is None:
        return None
    return {"risk_params": dict(cfg["risk_params"]), "mode": cfg["mode"], "capital_allocation_pct": cfg["capital_allocation_pct"]}


def get_all_index_configs() -> dict:
    return {idx: get_index_config(idx) for idx in INDICES}


def get_index_capital(index_id: str) -> float:
    """Actual rupee capital for this index right now, derived from its
    allocation % of total capital — always reflects the current total,
    never goes stale if total capital is later updated."""
    cfg = INDEX_CONFIG.get(index_id.upper())
    if cfg is None:
        return 0.0
    return round(ACCOUNT_CONFIG["total_available_capital"] * (cfg["capital_allocation_pct"] / 100.0), 2)


def update_index_risk_params(index_id: str, updates: dict) -> dict | None:
    cfg = INDEX_CONFIG.get(index_id.upper())
    if cfg is None:
        return None
    for key, value in updates.items():
        if key in cfg["risk_params"]:
            cfg["risk_params"][key] = value
    _persist_index_config()
    return get_index_config(index_id)


def update_index_mode(index_id: str, mode: str) -> dict | None:
    if mode not in ("paper", "live", "both", "off"):
        raise ValueError(f"mode must be one of paper/live/both/off, got '{mode}'")
    cfg = INDEX_CONFIG.get(index_id.upper())
    if cfg is None:
        return None
    cfg["mode"] = mode
    _persist_index_config()
    return get_index_config(index_id)


def update_capital_allocations(allocations: dict[str, float]) -> dict:
    """
    allocations: {"NIFTY50": 100} -- Nifty-only app, BankNifty/FinNifty
    removed. Validates they sum to ~100 (within a small tolerance)
    before applying; rejects and returns an error dict rather than
    silently normalizing, since silently rescaling could surprise the
    person setting this.
    """
    total = sum(allocations.values())
    if abs(total - 100.0) > 0.5:
        return {"status": "error", "message": f"Allocations must sum to 100%, got {total}%"}

    for index_id, pct in allocations.items():
        if index_id.upper() in INDEX_CONFIG:
            INDEX_CONFIG[index_id.upper()]["capital_allocation_pct"] = pct

    _persist_index_config()
    return {"status": "ok", "allocations": {idx: INDEX_CONFIG[idx]["capital_allocation_pct"] for idx in INDICES}}


def reset_index_config(index_id: str) -> dict | None:
    default = DEFAULT_INDEX_CONFIG.get(index_id.upper())
    if default is None:
        return None
    INDEX_CONFIG[index_id.upper()] = {
        "risk_params": dict(default["risk_params"]),
        "mode": default["mode"],
        "capital_allocation_pct": default["capital_allocation_pct"],
    }
    _persist_index_config()
    return get_index_config(index_id)


def reset_all_index_configs() -> dict:
    for index_id in INDICES:
        reset_index_config(index_id)  # each call already persists individually
    return get_all_index_configs()


def get_account_config() -> dict:
    return dict(ACCOUNT_CONFIG)


def update_account_config(updates: dict) -> dict:
    """
    Applies updates unconditionally, rather than only for keys already
    present in ACCOUNT_CONFIG. FIXED A REAL, CONFIRMED BUG: the
    previous version only updated a key `if key in ACCOUNT_CONFIG`,
    which silently did nothing for any key that wasn't already present
    in a server's in-memory config — e.g. a server whose ACCOUNT_CONFIG
    was loaded from a settings blob saved BEFORE live_trading_master_enabled
    existed would silently ignore every attempt to toggle it: the
    checkbox would show "on" right after clicking (pure frontend state),
    but nothing was ever actually saved, so the next page load correctly
    showed the real, unpersisted "off" state -- looking exactly like
    "the switch keeps turning itself off."
    """
    for key, value in updates.items():
        ACCOUNT_CONFIG[key] = value
    _persist_account_config()
    return get_account_config()