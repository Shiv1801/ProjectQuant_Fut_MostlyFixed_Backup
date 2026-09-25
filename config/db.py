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
    conn = get_connection()
    inserted = 0
    for c in candles:
        cur = conn.execute(
            """INSERT OR IGNORE INTO candles (index_id, minute_bucket, open, high, low, close, tick_count, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'historical')""",
            (index_id, c["minute_bucket"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0)),
        )
        inserted += cur.rowcount
    conn.commit()
    conn.close()
    return inserted


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


def get_iv_history_count(index_id: str) -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) as c FROM atm_iv_history WHERE index_id = ?", (index_id,)).fetchone()
    conn.close()
    return row["c"]


# ---------------- Positions (Step 36) ----------------

def open_position(index_id: str, direction: str, option_symbol: str, strike: float,
                   option_type: str, lots: int, lot_size: int, entry_price: float, mode: str = "paper") -> int:
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO positions
           (index_id, direction, option_symbol, strike, option_type, lots, lot_size,
            entry_price, entry_time, current_price, status, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (index_id, direction, option_symbol, strike, option_type, lots, lot_size,
         entry_price, now_ist().isoformat(), entry_price, mode),
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
    return position_id


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
                      pnl: float, pnl_pct: float, hold_minutes: float, mode: str = "paper"):
    conn = get_connection()
    conn.execute(
        """INSERT INTO trade_log
           (position_id, index_id, direction, option_symbol, strike, lots, entry_price,
            entry_time, exit_price, exit_time, exit_reason, pnl, pnl_pct, hold_minutes, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (position_id, index_id, direction, option_symbol, strike, lots, entry_price,
         entry_time, exit_price, exit_time, exit_reason, pnl, pnl_pct, hold_minutes, mode),
    )
    conn.commit()
    conn.close()


def get_trade_log(limit: int = 100):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM trade_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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