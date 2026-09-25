"""
PRD §2 — Cost model baked into every backtest, not bolted on after.
Brokerage, STT, exchange charges, SEBI fees, stamp duty, GST, and a
depth-derived slippage model (distinct for market vs limit orders).
Figures sourced from config/cost_model.yaml — verify current STT rate
against Fyers/exchange circulars before trusting old numbers.
"""
import yaml

with open("config/cost_model.yaml") as f:
    COST_PARAMS = yaml.safe_load(f)


def apply_transaction_costs(entry_price: float, exit_price: float, qty: int,
                             order_type: str = "market", liquidity_score: float = 1.0) -> float:
    gross_pnl = (exit_price - entry_price) * qty

    brokerage = COST_PARAMS["brokerage"]["amount_inr"] * 2  # entry + exit
    stt = exit_price * qty * COST_PARAMS["stt"]["sell_side_premium_pct"] / 100
    exch_charges = (entry_price + exit_price) * qty * COST_PARAMS["exchange_txn_charges_pct"] / 100
    sebi_fees = (entry_price + exit_price) * qty * COST_PARAMS["sebi_fees_pct"] / 100
    stamp_duty = entry_price * qty * COST_PARAMS["stamp_duty_pct"] / 100
    gst = brokerage * COST_PARAMS["gst_on_brokerage_pct"] / 100

    slippage = _estimate_slippage(entry_price, qty, order_type, liquidity_score)

    total_costs = brokerage + stt + exch_charges + sebi_fees + stamp_duty + gst + slippage
    return float(gross_pnl - total_costs)


def _estimate_slippage(price: float, qty: int, order_type: str, liquidity_score: float) -> float:
    base_bps = COST_PARAMS["slippage_model"][f"{order_type}_order_bps_base"]
    scaled_bps = base_bps / max(0.1, liquidity_score)  # thinner liquidity -> worse slippage
    return price * qty * scaled_bps / 10000
