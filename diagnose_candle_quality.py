"""Read-only diagnostic of NIFTY50 candle quality. Does not modify the database."""
import sqlite3
from pathlib import Path

db = Path("data/project_trade.db")
if not db.exists():
    raise FileNotFoundError(f"Run from project root; not found: {db}")

con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
try:
    print("=" * 76)
    print("NIFTY50 CANDLE QUALITY DIAGNOSTIC (READ-ONLY)")
    print("=" * 76)

    print("\n1) Overall quality")
    query = """
    SELECT COUNT(*) AS total,
      SUM(CASE WHEN open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL THEN 1 ELSE 0 END) AS null_ohlc,
      SUM(CASE WHEN open<=0 OR high<=0 OR low<=0 OR close<=0 THEN 1 ELSE 0 END) AS nonpositive_ohlc,
      SUM(CASE WHEN high < low OR high < open OR high < close OR low > open OR low > close THEN 1 ELSE 0 END) AS invalid_ohlc,
      SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END) AS null_volume,
      SUM(CASE WHEN volume=0 THEN 1 ELSE 0 END) AS zero_volume,
      SUM(CASE WHEN volume>0 THEN 1 ELSE 0 END) AS positive_volume,
      SUM(CASE WHEN open=high AND high=low AND low=close THEN 1 ELSE 0 END) AS all_prices_equal,
      SUM(CASE WHEN close=(SELECT close FROM candles c2 WHERE c2.index_id=c.index_id AND c2.minute_bucket < c.minute_bucket ORDER BY c2.minute_bucket DESC LIMIT 1) THEN 1 ELSE 0 END) AS same_close_as_previous_row
    FROM candles c WHERE index_id='NIFTY50'
    """
    row = con.execute(query).fetchone()
    print(dict(zip(
        ["total", "null_ohlc", "nonpositive_ohlc", "invalid_ohlc",
         "null_volume", "zero_volume", "positive_volume", "all_prices_equal",
         "same_close_as_previous_row"], row
    )))

    print("\n2) Rows per date: total, positive-volume, flat OHLC, distinct closes")
    for r in con.execute("""
      SELECT substr(minute_bucket,1,10) AS day,
             COUNT(*) AS total,
             SUM(CASE WHEN volume>0 THEN 1 ELSE 0 END) AS positive_volume,
             SUM(CASE WHEN open=high AND high=low AND low=close THEN 1 ELSE 0 END) AS flat_ohlc,
             COUNT(DISTINCT close) AS distinct_closes,
             MIN(minute_bucket) AS first_ts,
             MAX(minute_bucket) AS last_ts
      FROM candles WHERE index_id='NIFTY50'
      GROUP BY substr(minute_bucket,1,10)
      ORDER BY day DESC LIMIT 15
    """):
        print(r)

    print("\n3) Source distribution")
    for r in con.execute("""
      SELECT COALESCE(source,'<NULL>') AS source,
             COUNT(*) AS rows,
             SUM(CASE WHEN volume>0 THEN 1 ELSE 0 END) AS positive_volume,
             SUM(CASE WHEN open=high AND high=low AND low=close THEN 1 ELSE 0 END) AS flat_ohlc
      FROM candles WHERE index_id='NIFTY50'
      GROUP BY source ORDER BY rows DESC
    """):
        print(r)

    print("\n4) Latest 20 NIFTY50 rows")
    for r in con.execute("""
      SELECT minute_bucket, open, high, low, close, tick_count, source, volume
      FROM candles WHERE index_id='NIFTY50'
      ORDER BY minute_bucket DESC LIMIT 20
    """):
        print(r)

    print("\n5) Oldest 20 NIFTY50 rows")
    for r in con.execute("""
      SELECT minute_bucket, open, high, low, close, tick_count, source, volume
      FROM candles WHERE index_id='NIFTY50'
      ORDER BY minute_bucket LIMIT 20
    """):
        print(r)

    print("\n6) Most common (source, tick_count, volume) combinations")
    for r in con.execute("""
      SELECT COALESCE(source,'<NULL>'), tick_count, volume, COUNT(*)
      FROM candles WHERE index_id='NIFTY50'
      GROUP BY source, tick_count, volume
      ORDER BY COUNT(*) DESC LIMIT 20
    """):
        print(r)
finally:
    con.close()
