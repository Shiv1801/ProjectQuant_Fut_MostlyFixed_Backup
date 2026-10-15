"""Compatibility exports for the NIFTY futures OFI diagnostics."""

from engine.signal_layer.momentum.ofi import (
    calculate_futures_ofi,
    discover_nifty_monthly_futures,
)

__all__ = [
    "calculate_futures_ofi",
    "discover_nifty_monthly_futures",
]
