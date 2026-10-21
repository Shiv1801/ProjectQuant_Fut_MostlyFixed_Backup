"""
SQLite database. Schema changes are additive-only from here forward —
CREATE TABLE IF NOT EXISTS never touches existing tables, and new
columns are added via ALTER TABLE guarded by a try/except (SQLite has
no "ADD COLUMN IF NOT EXISTS"). This file should never again require
deleting data/project_trade.db to pick up a schema change.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path(__file__).parent.parent / "data" / "project_trade.db"
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode (2026-09-28): FIXES A REAL, CONFIRMED BUG. SQLite's default
    # journal mode gives a writer an exclusive lock on the WHOLE database
    # file -- every reader (auto_entry_loop, position_monitor_loop, even
    # the trivial /health-adjacent poller status reads) queues behind it.
    # Confirmed live: running scripts/backtest_sweep_nifty.py (200 rapid
    # writes to backtest_runs) made a completely unrelated, zero-Fyers-call
    # endpoint (/health) take ~20 SECONDS to respond, sustained even after
    # the sweep script had already printed "Done" and exited -- classic
    # writer-lock symptom, not a network or event-loop issue (same root
    # CLASS of bug as the fyers_apiv3 blocking-call fix from 2026-09-25,
    # but for SQLite instead of HTTP). WAL mode lets readers proceed
    # concurrently with a writer instead of queuing behind it -- a
    # standard, safe SQLite setting change, not a schema change.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _add_column_if_missing(conn, table: str, column: str, coltype: str):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise  # some other real error — don't swallow it


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ping_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            ltp REAL,
            change REAL,
            change_pct REAL,
            open REAL,
            high REAL,
            low REAL,
            prev_close REAL,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_index_ts ON ticks (index_id, ts DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            minute_bucket TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            tick_count INTEGER,
            UNIQUE(index_id, minute_bucket)
        )
    """)
    # 'source' column was added after candles already existed for some
    # users — ALTER TABLE guarded so it's safe whether or not it's there.
    _add_column_if_missing(conn, "candles", "source", "TEXT DEFAULT 'live'")
    # 'volume' column added 2026-10-05 -- FIXES A REAL, CONFIRMED BUG:
    # candles never had a genuine volume column at all. insert_historical_candles()
    # below was writing Fyers' real per-candle volume into the `tick_count`
    # column (a positional SQL/Python mismatch -- 7 placeholders including
    # one literally named tick_count in the SQL, fed from c.get("volume", 0)
    # in Python), so _compute_intraday_vwap()'s `c.get("volume")` read
    # always returned None even on backfilled rows, since no "volume" key
    # was ever actually stored. This adds the missing column so volume has
    # its own real place to live, separate from tick_count (which legitimately
    # tracks live-tick-update count for the in-progress current minute).
    _add_column_if_missing(conn, "candles", "volume", "REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_index_bucket ON candles (index_id, minute_bucket DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS atm_iv_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            minute_bucket TEXT NOT NULL,
            atm_strike REAL,
            atm_iv_pct REAL,
            UNIQUE(index_id, minute_bucket)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_iv_history_index_bucket ON atm_iv_history (index_id, minute_bucket DESC)")

    # Historical option-chain snapshot capture, added 2026-10-12, per
    # explicit request: ahead of any strike-selector scoring upgrade, this
    # table starts accumulating REAL per-strike chain data (bid/ask/OI/
    # volume/Greeks) going forward, so a future selector change can be
    # backtested against genuine historical market microstructure instead
    # of the Black-Scholes-synthesized approximation (engine/backtest/
    # engine_bs.py) that's all that's available today. Purely additive —
    # does not touch atm_iv_history, candles, or any other table, and is
    # NOT read by any live trading/sizing/execution code path.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS option_chain_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            minute_bucket TEXT NOT NULL,
            spot REAL,
            days_to_expiry REAL,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            symbol TEXT,
            ltp REAL,
            bid REAL,
            ask REAL,
            oi REAL,
            oi_change REAL,
            volume REAL,
            iv REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            UNIQUE(index_id, minute_bucket, strike, option_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_history_index_bucket ON option_chain_history (index_id, minute_bucket DESC)")

    # Step 36: paper/live position tracking + closed-trade log (PRD F14).
    # positions holds the full lifecycle of a trade (open -> closed);
    # trade_log is written once at close, purely for analytics/Kelly stats
    # (never mutated after insert — an append-only record).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            option_symbol TEXT NOT NULL,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            lots INTEGER NOT NULL,
            lot_size INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            current_price REAL,
            peak_profit_pct REAL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'open',
            exit_price REAL,
            exit_time TEXT,
            exit_reason TEXT,
            pnl REAL,
            pnl_pct REAL,
            mode TEXT NOT NULL DEFAULT 'paper',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_index_id ON positions (index_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            index_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            option_symbol TEXT NOT NULL,
            strike REAL NOT NULL,
            lots INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            exit_price REAL NOT NULL,
            exit_time TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            pnl_pct REAL NOT NULL,
            hold_minutes REAL,
            mode TEXT NOT NULL DEFAULT 'paper',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_log_index_id ON trade_log (index_id)")

    # Step 39: backtest run history — lets the UI compare tuning attempts
    # side by side instead of only ever seeing the most recent run.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            params_json TEXT NOT NULL,
            trade_count INTEGER,
            win_rate REAL,
            total_pnl REAL,
            return_pct REAL,
            max_drawdown_pct REAL,
            avg_win_pct REAL,
            avg_loss_pct REAL,
            applied_to_live INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_index_id ON backtest_runs (index_id)")

    # Persisted app settings (per-index risk config, account config) —
    # a simple key-value store so config survives a server restart.
    # Previously these lived ONLY in an in-memory Python dict, which
    # reset to hardcoded defaults every time uvicorn restarted — a real,
    # confirmed bug (settings appeared to "not save" because they were
    # never actually saved anywhere persistent in the first place).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Detailed, per-position action audit trail — every state change a
    # trade goes through (entry submitted, entry filled, TSL activated,
    # TSL trailed, exit triggered, exit filled, etc.), each with its
    # own exact timestamp and detail. Expanding a trade in the Trade
    # Log (open or closed) shows this full history.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            index_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_action_log_position ON trade_action_log (position_id, timestamp)")

    # Real order rejection log — separate from trade_log, since a
    # rejected order was NEVER a real trade (no position ever opened).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rejection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            attempted_action TEXT NOT NULL,
            reason TEXT NOT NULL,
            order_details_json TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rejection_log_timestamp ON rejection_log (timestamp DESC)")

    # Group G training data (added 2026-09-30): logs EVERY real
    # confluence-gate evaluation (fired AND rejected) with the full
    # Group A-F feature vector present at that moment, so a future GBT
    # classifier (engine/signal_layer/ml_ensemble/gbt_classifier.py) has
    # real, honest training data to learn from. This did NOT exist
    # before -- positions/trade_log record what trade happened and its
    # outcome, but never what the underlying model scores were AT ENTRY
    # TIME, so no backfill from past trades is possible; this table only
    # starts accumulating data from the moment logging went live.
    #
    # position_id is nullable and filled in LATER by link_model_score_to_position()
    # -- at the moment a score is computed, no trade has necessarily
    # opened yet (the gate may reject, or open_position() is several
    # function calls downstream in auto_entry_loop). A NULL position_id
    # after a reasonable window means the gate evaluation never led to a
    # real trade -- a genuinely useful NEGATIVE example for training
    # (what did a REJECTED setup look like), not a data gap.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            fired INTEGER NOT NULL,
            direction TEXT,
            veto_reason TEXT,
            quant_confidence REAL,
            chart_confirmed INTEGER,
            vwap_confirmed INTEGER,
            feature_vector_json TEXT NOT NULL,
            position_id INTEGER,
            linked_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_scores_index_ts ON model_scores (index_id, timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_scores_position ON model_scores (position_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_scores_unlinked ON model_scores (index_id, position_id, timestamp DESC)")

    # Added 2026-09-30, alongside Group G: records the Leg-1 quant
    # confidence score a trade actually fired at, directly on positions
    # and trade_log -- previously this value existed only transiently
    # inside confluence_signal's response and was never persisted
    # anywhere trade-level, so the Trade Log view had no way to show
    # "what confidence did this trade fire at" even though model_scores
    # (added earlier the same day) has the FULL feature vector, just not
    # joined into the Trade Log UI's normal query path. Nullable and
    # additive via _add_column_if_missing -- existing rows simply have
    # NULL here (genuinely unknown, not backfillable, since the score
    # was never recorded for trades that predate this column).
    _add_column_if_missing(conn, "positions", "quant_confidence", "REAL")
    _add_column_if_missing(conn, "trade_log", "quant_confidence", "REAL")

    conn.commit()
    conn.close()


def _ensure_app_settings_table(conn):
    """
    Creates the app_settings table if it doesn't exist yet. Called
    defensively from both save_setting and load_setting, since these
    can be invoked at MODULE IMPORT TIME (trading_config.py reads
    persisted settings as soon as it's imported, before FastAPI's
    on_startup handler has run init_db()) — a real ordering bug found
    when the app first crashed with "no such table: app_settings" on a
    fresh database. init_db() still creates this table too (so a
    normal startup is unaffected), this is just a safety net so
    save_setting/load_setting never depend on import ordering.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)


def save_setting(key: str, value: dict) -> None:
    """Persists any JSON-serializable config value under a string key."""
    import json
    conn = get_connection()
    _ensure_app_settings_table(conn)
    conn.execute(
        """INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = CURRENT_TIMESTAMP""",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def load_setting(key: str) -> dict | None:
    """Returns the persisted value for a key, or None if never saved."""
    import json
    conn = get_connection()
    _ensure_app_settings_table(conn)
    conn.commit()
    row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return None
    return json.loads(row["value_json"])


def insert_ping(message: str):
    conn = get_connection()
    conn.execute("INSERT INTO ping_log (message) VALUES (?)", (message,))
    conn.commit()
    conn.close()


def get_recent_pings(limit: int = 10):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, message, created_at FROM ping_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def insert_tick(index_id: str, quote: dict):
    conn = get_connection()
    conn.execute(
        """INSERT INTO ticks (index_id, ltp, change, change_pct, open, high, low, prev_close)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (index_id, quote.get("ltp"), quote.get("change"), quote.get("change_pct"),
         quote.get("open"), quote.get("high"), quote.get("low"), quote.get("prev_close")),
    )
    conn.commit()
    conn.close()

    _upsert_current_minute_candle(index_id, quote.get("ltp"))


def _upsert_current_minute_candle(index_id: str, ltp: float | None):
    if ltp is None:
        return
    minute_bucket = now_ist().strftime("%Y-%m-%d %H:%M:00")
    conn = get_connection()
    existing = conn.execute(
        "SELECT * FROM candles WHERE index_id = ? AND minute_bucket = ?", (index_id, minute_bucket)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO candles (index_id, minute_bucket, open, high, low, close, tick_count, source)
               VALUES (?, ?, ?, ?, ?, ?, 1, 'live')""",
            (index_id, minute_bucket, ltp, ltp, ltp, ltp),
        )
    else:
        new_high = max(existing["high"], ltp)
        new_low = min(existing["low"], ltp)
        conn.execute(
            """UPDATE candles SET high = ?, low = ?, close = ?, tick_count = tick_count + 1
               WHERE index_id = ? AND minute_bucket = ?""",
            (new_high, new_low, ltp, index_id, minute_bucket),
        )
    conn.commit()
    conn.close()


def insert_historical_candles(index_id: str, candles: list[dict]) -> int:
    """
    FIXED 2026-10-05, alongside the new `volume` column above: this
    previously wrote c.get("volume", 0) into the `tick_count` column
    (see the migration comment above `_add_column_if_missing(conn,
    "candles", "volume", ...)` for the full root-cause explanation) --
    volume is now written into its own real `volume` column, and
    tick_count for a historical row is correctly left NULL (historical
    rows aren't built from live ticks, so there is no real tick count
    for them -- 0 would have falsely implied "zero ticks seen").
    """
    conn = get_connection()
    inserted = 0
    for c in candles:
        cur = conn.execute(
            """INSERT OR IGNORE INTO candles (index_id, minute_bucket, open, high, low, close, volume, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'historical')""",
            (index_id, c["minute_bucket"], c["open"], c["high"], c["low"], c["close"], c.get("volume")),
        )
        inserted += cur.rowcount
    conn.commit()
    conn.close()
    return inserted


def update_candle_volume(index_id: str, minute_bucket: str, volume: float | None) -> bool:
    """
    Added 2026-10-05 for the new minute-boundary volume-enrichment pass
    (see engine/background/pollers.py's candle_volume_enrichment_loop).
    Updates ONLY the volume column of an already-existing candle row --
    never touches open/high/low/close/tick_count, which remain exactly
    as the live tick-built candle produced them. Does nothing (returns
    False) if no row exists yet for that minute_bucket -- this function
    never creates a candle row, only enriches one that the live
    tick-based path already created.
    """
    conn = get_connection()
    cur = conn.execute(
        "UPDATE candles SET volume = ? WHERE index_id = ? AND minute_bucket = ?",
        (volume, index_id, minute_bucket),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def get_recent_candles(index_id: str, limit: int = 60):
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM candles WHERE index_id = ?
           ORDER BY minute_bucket DESC LIMIT ?""", (index_id, limit)
    ).fetchall()
    conn.close()
    return [dict(row) for row in reversed(rows)]


def get_candle_count(index_id: str) -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as c FROM candles WHERE index_id = ?", (index_id,)).fetchone()
    conn.close()
    return row["c"]


def get_recent_ticks(index_id: str, limit: int = 20):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM ticks WHERE index_id = ? ORDER BY id DESC LIMIT ?", (index_id, limit)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_tick_count(index_id: str | None = None) -> int:
    conn = get_connection()
    if index_id:
        row = conn.execute("SELECT COUNT(*) as c FROM ticks WHERE index_id = ?", (index_id,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as c FROM ticks").fetchone()
    conn.close()
    return row["c"]


def insert_atm_iv_snapshot(index_id: str, atm_strike: float, atm_iv_pct: float):
    minute_bucket = now_ist().strftime("%Y-%m-%d %H:%M:00")
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO atm_iv_history (index_id, minute_bucket, atm_strike, atm_iv_pct)
           VALUES (?, ?, ?, ?)""",
        (index_id, minute_bucket, atm_strike, atm_iv_pct),
    )
    conn.commit()
    conn.close()


def get_iv_history(index_id: str, limit: int = 500):
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM atm_iv_history WHERE index_id = ?
           ORDER BY minute_bucket DESC LIMIT ?""", (index_id, limit)
    ).fetchall()
    conn.close()
    return [dict(row) for row in reversed(rows)]


def insert_option_chain_snapshot(index_id: str, minute_bucket: str, spot: float | None,
                                  days_to_expiry: float | None, rows: list[dict]):
    """
    rows: one dict per (strike, option_type) with keys symbol/ltp/bid/ask/
    oi/oi_change/volume/iv/delta/gamma/theta/vega — exactly the fields
    already parsed by _parse_chain_from_shared_state in api/main.py.
    INSERT OR REPLACE on the (index_id, minute_bucket, strike, option_type)
    unique key, same idempotent-per-minute pattern as insert_atm_iv_snapshot,
    so re-running the same minute (e.g. after a restart) never duplicates.
    """
    if not rows:
        return
    conn = get_connection()
    conn.executemany(
        """INSERT OR REPLACE INTO option_chain_history
           (index_id, minute_bucket, spot, days_to_expiry, strike, option_type, symbol,
            ltp, bid, ask, oi, oi_change, volume, iv, delta, gamma, theta, vega)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (index_id, minute_bucket, spot, days_to_expiry, r["strike"], r["option_type"], r.get("symbol"),
             r.get("ltp"), r.get("bid"), r.get("ask"), r.get("oi"), r.get("oi_change"), r.get("volume"),
             r.get("iv"), r.get("delta"), r.get("gamma"), r.get("theta"), r.get("vega"))
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def get_option_chain_history(index_id: str, start_bucket: str | None = None,
                              end_bucket: str | None = None, limit: int = 100000) -> list[dict]:
    """Read-only accessor for backtesting/analysis — not used by any live path."""
    conn = get_connection()
    query = "SELECT * FROM option_chain_history WHERE index_id = ?"
    params: list = [index_id]
    if start_bucket:
        query += " AND minute_bucket >= ?"
        params.append(start_bucket)
    if end_bucket:
        query += " AND minute_bucket <= ?"
        params.append(end_bucket)
    query += " ORDER BY minute_bucket ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_option_chain_history_count(index_id: str) -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as c FROM option_chain_history WHERE index_id = ?", (index_id,)).fetchone()
    conn.close()
    return row["c"]


def get_iv_history_count(index_id: str) -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as c FROM atm_iv_history WHERE index_id = ?", (index_id,)).fetchone()
    conn.close()
    return row["c"]


# ---------------- Positions (Step 36) ----------------

def open_position(index_id: str, direction: str, option_symbol: str, strike: float,
                   option_type: str, lots: int, lot_size: int, entry_price: float, mode: str = "paper",
                   quant_confidence: float | None = None) -> int:
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO positions
           (index_id, direction, option_symbol, strike, option_type, lots, lot_size,
            entry_price, entry_time, current_price, status, mode, quant_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (index_id, direction, option_symbol, strike, option_type, lots, lot_size,
         entry_price, now_ist().isoformat(), entry_price, mode, quant_confidence),
    )
    conn.commit()
    position_id = cursor.lastrowid
    conn.close()

    # Automatic, guaranteed action logging — every real position open
    # is logged here directly, so it can never be missed by a call
    # site forgetting to log it separately.
    log_trade_action(
        position_id, index_id, "entry_filled",
        f"{mode.upper()} | {direction} | strike={strike} {option_type} | lots={lots} | entry_price={entry_price}",
    )

    # Group G training-data link (added 2026-09-30): ties this real
    # trade back to the model_scores row that led to it (see
    # insert_model_score/link_model_score_to_position above). Best-
    # effort and NEVER allowed to fail trade entry itself -- a link
    # miss just means this one trade won't have training data attached
    # (e.g. a manual entry that never went through confluence_signal),
    # not a broken trade.
    try:
        link_model_score_to_position(index_id, position_id, direction)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"open_position: model_scores linking failed for position {position_id}: {e}")

    return position_id


def insert_model_score(index_id: str, fired: bool, direction: str | None, veto_reason: str | None,
                        quant_confidence: float | None, chart_confirmed: bool | None,
                        vwap_confirmed: bool | None, feature_vector: dict) -> int:
    """
    Logs ONE confluence-gate evaluation (Group G training data — added
    2026-09-30). Called from every real evaluation in api/main.py's
    confluence_signal(), for BOTH fired and rejected signals — a
    rejected setup (quant_below_threshold, chart_veto_opposite_structure,
    vwap_disagrees, etc.) is a genuinely useful negative training example,
    not noise to discard. feature_vector is the full Group A-F output
    dict (whatever was actually available at that moment — some fields
    may be None if e.g. option chain data wasn't ready yet), stored as
    JSON so the schema never has to change when a new model is added to
    Groups A-F. Returns the new row's id.
    """
    import json
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO model_scores
           (index_id, timestamp, fired, direction, veto_reason, quant_confidence,
            chart_confirmed, vwap_confirmed, feature_vector_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (index_id, now_ist().isoformat(), int(fired), direction, veto_reason, quant_confidence,
         None if chart_confirmed is None else int(chart_confirmed),
         None if vwap_confirmed is None else int(vwap_confirmed),
         json.dumps(feature_vector)),
    )
    conn.commit()
    score_id = cursor.lastrowid
    conn.close()
    return score_id


def link_model_score_to_position(index_id: str, position_id: int, direction: str,
                                  window_seconds: int = 30) -> int | None:
    """
    Called from open_position() (config/db.py) at the single guaranteed
    point a real trade opens, for BOTH paper and live. Links the
    model_scores row that led to this trade by finding the most recent
    UNLINKED, FIRED row for this index_id + direction within
    window_seconds of "now" -- since the real call chain (confluence_signal
    -> strike_selector_signal -> position_size_signal -> auto_entry_loop
    -> execute_paper_entry/execute_entry -> open_position) spans several
    function calls and a real Fyers order round-trip for live entries,
    there's no single variable to thread a position_id through cleanly;
    a short recency window on the same index+direction is a reliable,
    low-risk way to find the right row without rewriting every function
    signature in that chain.

    Returns the linked model_scores.id, or None if no matching unlinked
    row was found within the window (e.g. a manual/non-auto-entry trade
    that never went through confluence_signal at all -- logged as a gap,
    not silently ignored, since the caller can check for None).
    """
    conn = get_connection()
    cutoff = (now_ist() - timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        """SELECT id FROM model_scores
           WHERE index_id = ? AND direction = ? AND fired = 1 AND position_id IS NULL
             AND timestamp >= ?
           ORDER BY timestamp DESC LIMIT 1""",
        (index_id, direction, cutoff),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    score_id = row["id"]
    conn.execute(
        "UPDATE model_scores SET position_id = ?, linked_at = ? WHERE id = ?",
        (position_id, now_ist().isoformat(), score_id),
    )
    conn.commit()
    conn.close()
    return score_id


def get_model_scores_for_training(index_id: str | None = None, only_linked: bool = False):
    """
    Returns model_scores rows JOINED with their resulting trade outcome
    (pnl, pnl_pct, exit_reason) when linked — the actual training set for
    Group G. only_linked=True returns only rows tied to a real trade
    (needed for supervised training, since a label requires a real
    outcome); only_linked=False (default) also includes rejected/
    unlinked rows, useful for feature-distribution analysis even before
    there's a trained model.
    """
    import json
    conn = get_connection()
    query = """
        SELECT ms.*, p.pnl, p.pnl_pct, p.exit_reason, p.status AS position_status
        FROM model_scores ms
        LEFT JOIN positions p ON ms.position_id = p.id
        WHERE 1=1
    """
    params = []
    if index_id:
        query += " AND ms.index_id = ?"
        params.append(index_id)
    if only_linked:
        query += " AND ms.position_id IS NOT NULL AND p.status = 'closed'"
    query += " ORDER BY ms.timestamp ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["feature_vector"] = json.loads(d.pop("feature_vector_json"))
        results.append(d)
    return results


def get_model_scores_count(index_id: str | None = None, only_linked: bool = False) -> int:
    conn = get_connection()
    query = "SELECT COUNT(*) as c FROM model_scores WHERE 1=1"
    params = []
    if index_id:
        query += " AND index_id = ?"
        params.append(index_id)
    if only_linked:
        query += " AND position_id IS NOT NULL"
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row["c"]


def update_position_price(position_id: int, current_price: float, peak_profit_pct: float):
    conn = get_connection()
    conn.execute(
        "UPDATE positions SET current_price = ?, peak_profit_pct = ? WHERE id = ?",
        (current_price, peak_profit_pct, position_id),
    )
    conn.commit()
    conn.close()


def close_position(position_id: int, exit_price: float, exit_reason: str, pnl: float, pnl_pct: float):
    conn = get_connection()
    conn.execute(
        """UPDATE positions SET status = 'closed', exit_price = ?, exit_time = ?,
           exit_reason = ?, pnl = ?, pnl_pct = ? WHERE id = ?""",
        (exit_price, now_ist().isoformat(), exit_reason, pnl, pnl_pct, position_id),
    )
    conn.commit()

    # Fetch index_id for the action log entry — small extra read, but
    # guarantees this action is ALWAYS logged here, at the single real
    # point a position closes, regardless of caller.
    row = conn.execute("SELECT index_id FROM positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    index_id = row["index_id"] if row else "UNKNOWN"

    log_trade_action(
        position_id, index_id, "exit_filled",
        f"reason={exit_reason} | exit_price={exit_price} | pnl={pnl} | pnl_pct={pnl_pct}",
    )


def get_open_positions(index_id: str | None = None):
    conn = get_connection()
    if index_id:
        rows = conn.execute("SELECT * FROM positions WHERE status = 'open' AND index_id = ?", (index_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_position_by_id(position_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# get_position is an alias for get_position_by_id — added so
# api/main.py's manual-exit endpoint (which calls get_position) and
# any other code using either name both work correctly, rather than
# requiring every call site to agree on one exact name.
get_position = get_position_by_id


def log_trade_action(position_id: int, index_id: str, action: str, detail: str | None = None) -> None:
    """
    Records one timestamped action in a position's full audit trail —
    entry submitted, entry filled, TSL activated/trailed, exit
    triggered, exit filled, session-end/strike-scope forced exits,
    etc. Uses an explicit Python-generated ISO timestamp (millisecond
    precision) rather than SQLite's CURRENT_TIMESTAMP, which only has
    1-second resolution — too coarse for genuinely fast trade
    sequences where multiple real actions can land in the same second.
    """
    conn = get_connection()
    timestamp = now_ist().isoformat(timespec="milliseconds")
    conn.execute(
        "INSERT INTO trade_action_log (position_id, index_id, action, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
        (position_id, index_id, action, detail, timestamp),
    )
    conn.commit()
    conn.close()


def get_trade_action_log(position_id: int) -> list[dict]:
    """Full, chronological action history for one specific position —
    powers the "expand a trade to see detailed timestamps" view. Sorted
    by timestamp with id as a tiebreaker for same-millisecond actions."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trade_action_log WHERE position_id = ? ORDER BY timestamp ASC, id ASC", (position_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_rejection(index_id: str, attempted_action: str, reason: str, order_details: dict | None = None) -> None:
    """Records a real order that failed, was rejected, or never
    executed — separate from trade_log, since no position was ever
    actually opened."""
    import json
    conn = get_connection()
    timestamp = now_ist().isoformat(timespec="milliseconds")
    conn.execute(
        "INSERT INTO rejection_log (index_id, attempted_action, reason, order_details_json, timestamp) VALUES (?, ?, ?, ?, ?)",
        (index_id, attempted_action, reason, json.dumps(order_details) if order_details else None, timestamp),
    )
    conn.commit()
    conn.close()


def get_rejection_log(limit: int = 100) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM rejection_log ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- Trade Log (Step 36, PRD F14) ----------------

def insert_trade_log(position_id: int, index_id: str, direction: str, option_symbol: str,
                      strike: float, lots: int, entry_price: float, entry_time: str,
                      exit_price: float, exit_time: str, exit_reason: str,
                      pnl: float, pnl_pct: float, hold_minutes: float, mode: str = "paper",
                      quant_confidence: float | None = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO trade_log
           (position_id, index_id, direction, option_symbol, strike, lots, entry_price,
            entry_time, exit_price, exit_time, exit_reason, pnl, pnl_pct, hold_minutes, mode,
            quant_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (position_id, index_id, direction, option_symbol, strike, lots, entry_price,
         entry_time, exit_price, exit_time, exit_reason, pnl, pnl_pct, hold_minutes, mode,
         quant_confidence),
    )
    conn.commit()
    conn.close()


def get_trade_log(limit: int = 100, offset: int = 0):
    """
    offset added (2026-10-08, real server-side pagination fix): previously
    always returned only the N newest rows with no way to page further
    back, so once the table passed that count, older closed trades became
    permanently invisible in the UI even though they were never deleted.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trade_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def clear_trade_history(clear_positions: bool = True, clear_trade_log: bool = True) -> dict:
    """
    Deletes closed-trade history. Deliberately destructive and explicit
    — used e.g. after a bug fix (like the hold-time calculation) to
    start fresh with clean data, since old rows can't be retroactively
    corrected. Only clears trade_log and CLOSED positions by default;
    currently OPEN positions are left untouched unless explicitly asked
    for, since clearing an open position would orphan a live paper trade.
    """
    conn = get_connection()
    deleted = {}
    if clear_trade_log:
        cur = conn.execute("DELETE FROM trade_log")
        deleted["trade_log_rows_deleted"] = cur.rowcount
    if clear_positions:
        cur = conn.execute("DELETE FROM positions WHERE status = 'closed'")
        deleted["closed_positions_deleted"] = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_trade_log_stats():
    """Aggregate win rate / avg win / avg loss — feeds Group F's Kelly sizing."""
    conn = get_connection()
    rows = conn.execute("SELECT pnl, pnl_pct FROM trade_log").fetchall()
    conn.close()

    trades = [dict(r) for r in rows]
    if not trades:
        return {"trade_count": 0, "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None}

    wins = [t["pnl_pct"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl_pct"]) for t in trades if t["pnl"] <= 0]

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win_pct": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 3) if losses else None,
    }


def get_trade_log_detailed_stats(index_id: str | None = None, start_date: str | None = None,
                                  end_date: str | None = None):
    """
    Richer statistics for the Trade Statistics UI — everything
    get_trade_log_stats() has, plus profit factor, best/worst trade,
    max drawdown of the equity curve, and the equity curve itself
    (cumulative P&L over time, one point per closed trade, in
    chronological order) so the frontend can render a real chart
    without recomputing anything client-side.

    Optional filters (index_id, start_date, end_date) let the same
    function serve both the "all trades" default view and a filtered
    view, so the frontend's Filter/Reset controls can just re-call this
    endpoint with different query params rather than needing a second
    code path.
    """
    conn = get_connection()
    query = "SELECT * FROM trade_log WHERE 1=1"
    params = []
    if index_id:
        query += " AND index_id = ?"
        params.append(index_id)
    if start_date:
        query += " AND exit_time >= ?"
        params.append(start_date)
    if end_date:
        query += " AND exit_time <= ?"
        params.append(end_date)
    query += " ORDER BY exit_time ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    trades = [dict(r) for r in rows]

    if not trades:
        return {
            "trade_count": 0, "win_rate": None, "total_pnl": 0.0, "avg_win_pct": None, "avg_loss_pct": None,
            "profit_factor": None, "best_trade_pnl": None, "worst_trade_pnl": None,
            "max_drawdown_pct": None, "max_drawdown_amount": None, "equity_curve": [],
        }

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    # Equity curve: cumulative P&L after each trade, in chronological order.
    equity_curve = []
    running_pnl = 0.0
    peak = 0.0
    max_dd_amount = 0.0
    max_dd_pct_val = 0.0
    for t in trades:
        running_pnl += t["pnl"]
        peak = max(peak, running_pnl)
        drawdown_amount = peak - running_pnl
        drawdown_pct = (drawdown_amount / peak * 100) if peak > 0 else 0.0
        max_dd_amount = max(max_dd_amount, drawdown_amount)
        max_dd_pct_val = max(max_dd_pct_val, drawdown_pct)
        equity_curve.append({
            "exit_time": t["exit_time"], "trade_pnl": round(t["pnl"], 2),
            "cumulative_pnl": round(running_pnl, 2), "index_id": t["index_id"],
        })

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "total_pnl": round(total_pnl, 2),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(abs(t["pnl_pct"]) for t in losses) / len(losses), 3) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "best_trade_pnl": round(max(t["pnl"] for t in trades), 2),
        "worst_trade_pnl": round(min(t["pnl"] for t in trades), 2),
        "max_drawdown_pct": round(max_dd_pct_val, 2),
        "max_drawdown_amount": round(max_dd_amount, 2),
        "equity_curve": equity_curve,
    }


# ---------------- Backtest Run History (Step 39) ----------------

def insert_backtest_run(index_id: str, mode: str, params: dict, summary: dict | None) -> int:
    import json
    conn = get_connection()
    summary = summary or {}
    cursor = conn.execute(
        """INSERT INTO backtest_runs
           (index_id, mode, params_json, trade_count, win_rate, total_pnl,
            return_pct, max_drawdown_pct, avg_win_pct, avg_loss_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (index_id, mode, json.dumps(params), summary.get("trade_count"), summary.get("win_rate"),
         summary.get("total_pnl"), summary.get("return_pct"), summary.get("max_drawdown_pct"),
         summary.get("avg_win_pct"), summary.get("avg_loss_pct")),
    )
    conn.commit()
    run_id = cursor.lastrowid
    conn.close()
    return run_id


def get_backtest_runs(index_id: str | None = None, limit: int = 20):
    import json
    conn = get_connection()
    if index_id:
        rows = conn.execute("SELECT * FROM backtest_runs WHERE index_id = ? ORDER BY id DESC LIMIT ?", (index_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json"))
        results.append(d)
    return results


def mark_backtest_run_applied(run_id: int):
    conn = get_connection()
    conn.execute("UPDATE backtest_runs SET applied_to_live = 1 WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()