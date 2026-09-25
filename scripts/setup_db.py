"""Runs migrations against TIMESCALE_DSN. See migrations/001_initial_schema.sql."""
import asyncio
import asyncpg

from config.settings import settings


async def main():
    conn = await asyncpg.connect(settings.timescale_dsn)
    with open("migrations/001_initial_schema.sql") as f:
        sql = f.read()
    await conn.execute(sql)
    print("Schema applied.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
