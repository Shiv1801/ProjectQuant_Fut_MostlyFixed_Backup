"""
PRD §4.6 — Tick-accurate replay engine, NOT OHLC-bar backtesting.
Bar-level backtests hide slippage/theta reality for options. This replays
stored ticks + option chain snapshots in exact chronological order and
feeds them through the same confluence_gate / risk_engine code paths
used live, so backtest and live share logic (no parallel reimplementation).
"""
from dataclasses import dataclass
from datetime import datetime

from engine.confluence_gate.gate import evaluate_confluence
from engine.risk_engine.stop_loss import check_initial_stop_loss
from engine.risk_engine.trailing_stop import update_tsl, check_tsl_exit
from engine.backtest.cost_model.costs import apply_transaction_costs


@dataclass
class BacktestResult:
    trades: list
    sharpe: float
    profit_factor: float
    win_rate: float
    max_dd: float


class TickReplayEngine:
    """
    Critical: must guard against lookahead bias explicitly (PRD §7.4 gap) —
    at simulated timestamp T, only data with ts <= T may be visible to any
    model. Feed data via a generator that enforces this, never index-ahead
    into stored arrays.
    """

    def __init__(self, cost_params: dict, risk_params: dict):
        self.cost_params = cost_params
        self.risk_params = risk_params

    def run(self, tick_stream, option_chain_stream, models: dict) -> BacktestResult:
        trades = []
        open_positions = {}

        for event in self._merged_chronological_stream(tick_stream, option_chain_stream):
            # Only data up to event.ts is ever passed into models — no future leakage
            self._process_open_positions(event, open_positions, trades)
            self._maybe_evaluate_new_signal(event, models, open_positions)

        return self._summarize(trades)

    def _merged_chronological_stream(self, tick_stream, option_chain_stream):
        # Merge-sort two time-ordered iterators by timestamp
        raise NotImplementedError("Implement chronological merge of tick + chain snapshots")

    def _process_open_positions(self, event, open_positions, trades):
        raise NotImplementedError("Apply SL/TSL/time-stop checks against this event's prices")

    def _maybe_evaluate_new_signal(self, event, models, open_positions):
        raise NotImplementedError("Run confluence gate on 1-min bar close events only, per §4.3.5")

    def _summarize(self, trades) -> BacktestResult:
        raise NotImplementedError("Compute sharpe/profit_factor/win_rate/max_dd from trades list")
