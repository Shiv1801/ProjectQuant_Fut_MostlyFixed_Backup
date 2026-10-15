"""Builds the standalone HTML dashboard for the index-mode backtest,
embedding the backtest's own result JSON directly into the page (no
fetch needed, so it works opened straight from disk via file://).

Run scripts/backtest_index_mode.py FIRST -- this reads the
scripts/backtest_index_mode_result.json file that script writes."""
import json
import os

RESULT_PATH = "scripts/backtest_index_mode_result.json"
if not os.path.exists(RESULT_PATH):
    raise SystemExit(
        f"\n{RESULT_PATH} not found.\n"
        f"Run the backtest first:  python -m scripts.backtest_index_mode\n"
        f"Then run this script again.\n"
    )

with open(RESULT_PATH) as f:
    full_result = json.load(f)

dashboard_data = {
    "settings": full_result["settings"],
    "summary": full_result["summary"],
    "trades": full_result["trades"],
}
data_json = json.dumps(dashboard_data, separators=(",", ":"))
