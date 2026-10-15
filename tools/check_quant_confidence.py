"""
Read-only diagnostic: checks why recent trade_log rows have NULL
quant_confidence. Run from your project root:
    python tools/check_quant_confidence.py
"""
import sqlite3

conn = sqlite3.connect("data/project_trade.db")
conn.row_factory = sqlite3.Row

print("=== Last 10 trade_log rows (quant_confidence check) ===")
rows = conn.execute(
    "SELECT id, position_id, index_id, direction, exit_reason, mode, quant_confidence, exit_time "
    "FROM trade_log ORDER BY id DESC LIMIT 10"
).fetchall()
for r in rows:
    print(dict(r))

print()
print("=== Corresponding positions rows (was quant_confidence set at OPEN time?) ===")
position_ids = [r["position_id"] for r in rows]
for pid in position_ids:
    p = conn.execute("SELECT id, index_id, mode, quant_confidence, entry_time FROM positions WHERE id = ?", (pid,)).fetchone()
    print(dict(p) if p else {"position_id": pid, "note": "not found in positions table"})

conn.close()
