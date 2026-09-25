-- Project Trade initial schema — mirrors PRD §7.1 (F1-F14)
-- Requires TimescaleDB extension for hypertables.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- F1: Live Multi-Index Chart Engine
CREATE TABLE IF NOT EXISTS candles (
    index_id     TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    ts_open      TIMESTAMPTZ NOT NULL,
    open         DOUBLE PRECISION,
    high         DOUBLE PRECISION,
    low          DOUBLE PRECISION,
    close        DOUBLE PRECISION,
    volume       BIGINT,
    tick_count   INT,
    PRIMARY KEY (index_id, timeframe, ts_open)
);
SELECT create_hypertable('candles', 'ts_open', if_not_exists => TRUE);

-- F2: Tick & Historical Store
CREATE TABLE IF NOT EXISTS ticks (
    index_id  TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    ltp       DOUBLE PRECISION,
    ltq       INT,
    bid       DOUBLE PRECISION,
    ask       DOUBLE PRECISION,
    bid_qty   INT,
    ask_qty   INT,
    oi        BIGINT
);
SELECT create_hypertable('ticks', 'ts', if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day');
CREATE INDEX IF NOT EXISTS idx_ticks_index_ts ON ticks (index_id, ts DESC);

-- F3: Option Chain Snapshot Store
CREATE TABLE IF NOT EXISTS option_chain (
    index_id   TEXT NOT NULL,
    expiry     TEXT NOT NULL,
    strike     DOUBLE PRECISION NOT NULL,
    opt_type   TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    ltp        DOUBLE PRECISION,
    bid        DOUBLE PRECISION,
    ask        DOUBLE PRECISION,
    oi         BIGINT,
    oi_change  BIGINT,
    volume     BIGINT,
    iv         DOUBLE PRECISION,
    delta      DOUBLE PRECISION,
    gamma      DOUBLE PRECISION,
    theta      DOUBLE PRECISION,
    vega       DOUBLE PRECISION
);
SELECT create_hypertable('option_chain', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_chain_index_expiry_strike ON option_chain (index_id, expiry, strike, ts DESC);

-- F4: Market Depth
CREATE TABLE IF NOT EXISTS depth (
    instrument_id TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    side          TEXT NOT NULL,
    level         INT NOT NULL,
    price         DOUBLE PRECISION,
    qty           INT,
    num_orders    INT
);
SELECT create_hypertable('depth', 'ts', if_not_exists => TRUE);

-- F5: VIX & Regime Tracker
CREATE TABLE IF NOT EXISTS vix (
    ts          TIMESTAMPTZ NOT NULL PRIMARY KEY,
    ltp         DOUBLE PRECISION,
    roc_1min    DOUBLE PRECISION,
    roc_5min    DOUBLE PRECISION,
    roc_daily   DOUBLE PRECISION
);
SELECT create_hypertable('vix', 'ts', if_not_exists => TRUE);

-- F6: Quant Model Engine
CREATE TABLE IF NOT EXISTS model_scores (
    index_id           TEXT NOT NULL,
    ts                 TIMESTAMPTZ NOT NULL,
    model_id           TEXT NOT NULL,
    score              DOUBLE PRECISION,
    confidence         DOUBLE PRECISION,
    feature_vector_json JSONB
);
SELECT create_hypertable('model_scores', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_model_scores_lookup ON model_scores (index_id, model_id, ts DESC);

-- F7: Entry Confluence Gate
CREATE TABLE IF NOT EXISTS signal_log (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts             TIMESTAMPTZ NOT NULL,
    index_id       TEXT NOT NULL,
    direction      TEXT,
    quant_score    DOUBLE PRECISION,
    chart_confirmed BOOLEAN,
    veto_reason    TEXT,
    fired          BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_log_ts ON signal_log (ts DESC);

-- F8: Strike Selector
CREATE TABLE IF NOT EXISTS strike_selection (
    signal_id        UUID REFERENCES signal_log(id),
    chosen_strike    DOUBLE PRECISION,
    chosen_expiry    TEXT,
    delta_at_entry   DOUBLE PRECISION,
    liquidity_score  DOUBLE PRECISION,
    iv_rank_at_entry DOUBLE PRECISION
);

-- F9: Position & Order Manager
CREATE TABLE IF NOT EXISTS positions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mode               TEXT NOT NULL,       -- paper | live
    index_id           TEXT NOT NULL,
    strike             DOUBLE PRECISION,
    expiry             TEXT,
    opt_type           TEXT,
    qty                INT,
    entry_price        DOUBLE PRECISION,
    entry_ts           TIMESTAMPTZ,
    current_sl         DOUBLE PRECISION,
    current_tsl_floor  DOUBLE PRECISION,
    status             TEXT,                -- open | closed
    exit_price         DOUBLE PRECISION,
    exit_ts            TIMESTAMPTZ,
    exit_reason        TEXT,
    pnl                DOUBLE PRECISION,
    pnl_pct            DOUBLE PRECISION
);

-- F10: In-Trade Rejection Monitor (Model E)
CREATE TABLE IF NOT EXISTS rejection_checks (
    position_id            UUID REFERENCES positions(id),
    ts                      TIMESTAMPTZ NOT NULL,
    delta_direction_ok      BOOLEAN,
    ofi_aligned             BOOLEAN,
    chart_structure_verdict TEXT,
    action_taken            TEXT
);

-- F11: Risk & Circuit Breaker Engine
CREATE TABLE IF NOT EXISTS risk_state (
    date                              DATE NOT NULL,
    mode                              TEXT NOT NULL,
    daily_pnl                         DOUBLE PRECISION,
    daily_pnl_pct                     DOUBLE PRECISION,
    consecutive_losses                INT,
    current_position_size_multiplier  DOUBLE PRECISION,
    breaker_tripped                   BOOLEAN,
    breaker_reason                    TEXT,
    PRIMARY KEY (date, mode)
);

-- F12: Backtest Engine
CREATE TABLE IF NOT EXISTS backtest_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_ids     TEXT[],
    date_range    TSTZRANGE,
    mode          TEXT,
    results_json  JSONB,
    sharpe        DOUBLE PRECISION,
    profit_factor DOUBLE PRECISION,
    win_rate      DOUBLE PRECISION,
    max_dd        DOUBLE PRECISION,
    created_ts    TIMESTAMPTZ DEFAULT now()
);

-- F13: ML Retrain & Champion/Challenger Pipeline
CREATE TABLE IF NOT EXISTS model_versions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id         TEXT NOT NULL,
    version          INT NOT NULL,
    trained_on_range TSTZRANGE,
    params_json      JSONB,
    oos_score        DOUBLE PRECISION,
    promoted         BOOLEAN DEFAULT FALSE,
    promoted_ts      TIMESTAMPTZ
);

-- F14: Trade Log & Analytics
CREATE TABLE IF NOT EXISTS trade_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date             DATE NOT NULL,
    symbol           TEXT NOT NULL,
    strike           DOUBLE PRECISION,
    opt_type         TEXT,
    expiry           TEXT,
    qty              INT,
    entry_price      DOUBLE PRECISION,
    entry_ts         TIMESTAMPTZ,
    exit_price       DOUBLE PRECISION,
    exit_ts          TIMESTAMPTZ,
    holding_time_sec INT,
    pnl_inr          DOUBLE PRECISION,
    pnl_pct          DOUBLE PRECISION,
    exit_reason      TEXT CHECK (exit_reason IN
        ('tsl_hit','hard_sl_hit','time_stop','early_exit_rejection','expiry_force_exit','manual_kill_switch')),
    mode             TEXT,
    signal_id        UUID REFERENCES signal_log(id)
);
CREATE INDEX IF NOT EXISTS idx_trade_log_date ON trade_log (date DESC);
