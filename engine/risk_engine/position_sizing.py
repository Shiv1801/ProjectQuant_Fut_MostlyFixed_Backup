"""
Position sizing, now using REAL Fyers account balance when available,
falling back honestly to the manually-configured Settings capital
when real data isn't available (e.g. before the first successful
account_balance_poller fetch, or during a Fyers outage).
"""
LOT_SIZES = {"NIFTY50": 65}
# FIXED per a REAL Fyers rejection received in live trading:
# "150 not a multiple of minimum lot size 65" -- confirms NSE's current
# Nifty lot size is 65, not 75. NSE periodically revises lot sizes;
# this was stale. Verify against NSE's current contract specifications
# periodically, since this can change again.


def get_effective_capital(index_id: str | None = None) -> float:
    """
    The single, real source of truth for "how much capital do we
    actually have to size a new position against".

    FIXED A REAL BUG: this previously used the real Fyers balance
    whenever it was not None -- but a real balance of exactly 0 (a
    genuine, correctly-fetched value, not "unavailable") was being
    used for PAPER trades too, silently sizing every paper position at
    0 lots. Real balance should only ever apply when live trading is
    actually active for the given index -- paper trading must always
    use the configured paper capital (e.g. Rs 50,000), regardless of
    what the real account balance happens to be.

    index_id: when provided, checks whether live trading is genuinely
    active for THIS index (both safety gates) before using the real
    balance. When omitted (e.g. a caller with no specific index in
    context), falls back to the old real-balance-if-available
    behavior, since there's no way to know which mode applies.
    """
    from config.trading_config import get_account_config
    account = get_account_config()

    if index_id is not None:
        from engine.execution.trade_dispatcher import is_live_trading_active_for
        if is_live_trading_active_for(index_id):
            real_balance = account.get("real_available_balance")
            if real_balance is not None:
                return real_balance
        return account.get("total_available_capital", 0.0)

    real_balance = account.get("real_available_balance")
    if real_balance is not None:
        return real_balance
    return account.get("total_available_capital", 0.0)


def compute_position_size(available_capital: float, index_id: str, premium_per_share: float,
                           hard_sl_pct: float, risk_per_trade_pct: float,
                           kelly_scale: float | None = None, vol_scale: float | None = None) -> dict:
    """
    kelly_scale: an optional multiplier derived from the Kelly Criterion
    sizing model (api/main.py's position_size_signal computes this from
    kelly_sizing_signal's sized_fraction, scaled to ~1.0x around a
    neutral point). vol_scale: an optional multiplier from the
    volatility-scaled sizing model. Both default to None (neutral,
    1.0x) when unavailable -- e.g. before those models have enough
    history to compute a real value -- so the base risk_per_trade_pct
    sizing still works standalone. FIXED A REAL BUG: an earlier version
    of this function dropped both parameters entirely, causing
    api/main.py's real position_size_signal endpoint (which has always
    passed them) to crash with "unexpected keyword argument
    'kelly_scale'" on every call.
    """
    if premium_per_share <= 0 or hard_sl_pct <= 0:
        return {"lots": 0, "reason": "invalid premium or SL"}

    lot_size = LOT_SIZES.get(index_id)
    if not lot_size:
        return {"lots": 0, "reason": f"unknown lot size for {index_id}"}

    effective_risk_pct = risk_per_trade_pct
    if kelly_scale is not None:
        effective_risk_pct *= kelly_scale
    if vol_scale is not None:
        effective_risk_pct *= vol_scale

    risk_amount = available_capital * (effective_risk_pct / 100)
    max_loss_per_share = premium_per_share * (hard_sl_pct / 100)
    if max_loss_per_share <= 0:
        return {"lots": 0, "reason": "zero max loss per share"}

    max_shares = risk_amount / max_loss_per_share
    lots = int(max_shares // lot_size)

    while lots > 0 and (lots * lot_size * premium_per_share) > available_capital:
        lots -= 1

    capital_required = lots * lot_size * premium_per_share
    capital_required_pct = (capital_required / available_capital * 100) if available_capital > 0 else 0

    return {
        "lots": lots, "lot_size": lot_size,
        "capital_required": round(capital_required, 2),
        "capital_required_pct_of_available": round(capital_required_pct, 1),
        "effective_risk_per_trade_pct": round(effective_risk_pct, 3),
        "kelly_scale_applied": kelly_scale, "vol_scale_applied": vol_scale,
    }