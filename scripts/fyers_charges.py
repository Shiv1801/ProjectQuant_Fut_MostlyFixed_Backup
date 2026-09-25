"""
Fyers Options Trading Charges Calculator — real, current charge
structure, sourced directly from Fyers' official charges page
(https://fyers.in/charges-list, fetched fresh, not estimated):

  - Brokerage: flat Rs 20 per executed order (buy and sell are TWO
    separate orders, so a round-trip trade pays this twice)
  - STT (Securities Transaction Tax): 0.05% on SELL-side value only
  - Exchange Transaction Charges (NSE): 0.0355299% on premium value,
    charged on BOTH buy and sell legs
  - SEBI Turnover Fee: Rs 10 per crore of turnover, on BOTH legs
  - Stamp Duty: 0.003% on BUY value only
  - GST: 18% on (Brokerage + Exchange Transaction Charges + SEBI fee) —
    NOT applied to STT or Stamp Duty, which are themselves statutory
    levies rather than taxable services

Turnover for a leg = premium x lots x lot_size (the real notional
value traded on that leg).
"""

BROKERAGE_PER_ORDER = 20.0
STT_RATE_SELL_SIDE = 0.0005  # 0.05%
EXCHANGE_TXN_CHARGE_RATE = 0.000355299  # 0.0355299%, on premium
SEBI_TURNOVER_FEE_RATE = 10.0 / 10_000_000  # Rs 10 per crore = Rs 10 / 1,00,00,000
STAMP_DUTY_RATE_BUY_SIDE = 0.00003  # 0.003%
GST_RATE = 0.18


def compute_round_trip_charges(entry_premium: float, exit_premium: float, lots: int, lot_size: int) -> dict:
    """
    Computes every real charge component for one complete trade (one
    buy order to open, one sell order to close), and returns both the
    itemized breakdown and the total, so it's auditable rather than a
    black-box deduction.
    """
    buy_turnover = entry_premium * lots * lot_size
    sell_turnover = exit_premium * lots * lot_size

    brokerage = BROKERAGE_PER_ORDER * 2  # one order to open, one to close

    stt = sell_turnover * STT_RATE_SELL_SIDE

    exchange_txn_charges = (buy_turnover + sell_turnover) * EXCHANGE_TXN_CHARGE_RATE

    sebi_fee = (buy_turnover + sell_turnover) * SEBI_TURNOVER_FEE_RATE

    stamp_duty = buy_turnover * STAMP_DUTY_RATE_BUY_SIDE

    gst = (brokerage + exchange_txn_charges + sebi_fee) * GST_RATE

    total_charges = brokerage + stt + exchange_txn_charges + sebi_fee + stamp_duty + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn_charges": round(exchange_txn_charges, 2),
        "sebi_fee": round(sebi_fee, 2),
        "stamp_duty": round(stamp_duty, 2),
        "gst": round(gst, 2),
        "total_charges": round(total_charges, 2),
    }
