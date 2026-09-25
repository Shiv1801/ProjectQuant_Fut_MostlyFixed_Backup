"""
Python-side mirror of the DB schema (PRD §7.1, F1-F14) using dataclasses.
Source of truth for column names/types is migrations/001_initial_schema.sql —
keep these in sync manually or regenerate via scripts/gen_models_from_db.py.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Tick:
    index_id: str
    ts: datetime
    ltp: float
    ltq: int
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    oi: int


@dataclass
class OptionChainRow:
    index_id: str
    expiry: str
    strike: float
    opt_type: str  # CE | PE
    ts: datetime
    ltp: float
    bid: float
    ask: float
    oi: int
    oi_change: int
    volume: int
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass
class SignalLogRow:
    id: str
    ts: datetime
    index_id: str
    direction: str  # long_call | long_put
    quant_score: float
    chart_confirmed: bool
    veto_reason: str | None
    fired: bool


@dataclass
class PositionRow:
    id: str
    mode: str  # paper | live
    index_id: str
    strike: float
    expiry: str
    opt_type: str
    qty: int
    entry_price: float
    entry_ts: datetime
    current_sl: float
    current_tsl_floor: float
    status: str  # open | closed
    exit_price: float | None
    exit_ts: datetime | None
    exit_reason: str | None  # enum, see TradeLogRow
    pnl: float | None
    pnl_pct: float | None


@dataclass
class TradeLogRow:
    id: str
    date: datetime
    symbol: str
    strike: float
    opt_type: str
    expiry: str
    qty: int
    entry_price: float
    entry_ts: datetime
    exit_price: float
    exit_ts: datetime
    holding_time_sec: int
    pnl_inr: float
    pnl_pct: float
    exit_reason: str  # tsl_hit | hard_sl_hit | time_stop | early_exit_rejection | expiry_force_exit | manual_kill_switch
    mode: str
    signal_id: str
