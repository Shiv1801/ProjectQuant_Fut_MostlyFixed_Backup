"""
Group C: Order Flow Imbalance (PRD §3.5-C). Uses total bid vs ask
quantity from the live market depth (5-level) to measure buy/sell
pressure imbalance. Previously blocked pending WebSocket depth data —
unblocked using Fyers' REST /data/depth endpoint (confirmed against
official docs), which gives totalbuyqty/totalsellqty directly.
"""


def compute_ofi(total_buy_qty: int | None, total_sell_qty: int | None) -> dict:
    if total_buy_qty is None or total_sell_qty is None:
        return {"ofi": None, "note": "missing depth data"}

    total = total_buy_qty + total_sell_qty
    if total == 0:
        return {
            "ofi": 0.0, "total_buy_qty": 0, "total_sell_qty": 0,
            "note": "no depth volume — this is expected when the market is closed "
                    "(9:15 AM - 3:30 PM IST on trading days) or right at open/close; "
                    "not necessarily a data problem if checked outside those hours",
        }

    ofi = (total_buy_qty - total_sell_qty) / total  # range: -1 (all sell pressure) to +1 (all buy pressure)

    if ofi > 0.2:
        interpretation = "buy pressure dominant"
    elif ofi < -0.2:
        interpretation = "sell pressure dominant"
    else:
        interpretation = "balanced order flow"

    return {
        "ofi": round(ofi, 3),
        "total_buy_qty": total_buy_qty,
        "total_sell_qty": total_sell_qty,
        "interpretation": interpretation,
        "note": None,
    }
