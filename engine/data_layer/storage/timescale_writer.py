"""
Append-only writers into TimescaleDB hypertables. Every table maps 1:1
to a PRD §7.1 schema (F1-F14). Writers here are the ONLY place data
enters storage — this is the ingestion side of the §7.3b single-source
-of-truth architecture. Nothing else in the codebase should INSERT
into these tables directly.
"""
import asyncpg
from loguru import logger

from config.settings import settings


class TimescaleWriter:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def init_pool(self):
        self.pool = await asyncpg.create_pool(settings.timescale_dsn)

    async def write_tick(self, index_id, ts, ltp, ltq, bid, ask, bid_qty, ask_qty, oi):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ticks (index_id, ts, ltp, ltq, bid, ask, bid_qty, ask_qty, oi)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                index_id, ts, ltp, ltq, bid, ask, bid_qty, ask_qty, oi,
            )

    async def write_option_chain_snapshot(self, row: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO option_chain
                   (index_id, expiry, strike, opt_type, ts, ltp, bid, ask, oi,
                    oi_change, volume, iv, delta, gamma, theta, vega)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                *row.values(),
            )

    async def write_trade_log(self, row: dict):
        """Written exactly once, synchronously, at trade close (PRD F14)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO trade_log
                   (date, symbol, strike, opt_type, expiry, qty, entry_price, entry_ts,
                    exit_price, exit_ts, holding_time_sec, pnl_inr, pnl_pct, exit_reason,
                    mode, signal_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                *row.values(),
            )
            logger.info(f"trade_log written: {row.get('symbol')} pnl={row.get('pnl_inr')}")
