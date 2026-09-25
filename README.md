# Project Trade — Option-Buying Algobot

Long-premium (option buying) automated trading system for Nifty / BankNifty / FinNifty.
See `docs/PRD.md` for the full product spec this codebase implements.

## Quick start

```bash
cp .env.example .env          # fill in Fyers credentials
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python scripts/setup_db.py
python scripts/fyers_login.py        # one-time daily token refresh
python scripts/run_paper.py          # start in Paper mode (default, safe)
```

## Repo map

| Path | Purpose |
|---|---|
| `engine/` | Core trading engine — data, signals, models, risk, execution (§3 architecture) |
| `api/` | FastAPI backend exposing engine state to frontend (§7.3b shared-state reads) |
| `frontend/` | React dashboard — 6 tabs (§7.2) |
| `desktop_client/` | Tauri thin client wrapping the frontend (§10.3) |
| `scripts/` | Operational scripts (login, backtest runner, retrain trigger) |
| `tests/` | Unit, integration, and backtest-validation tests |
| `migrations/` | TimescaleDB schema migrations (§7.1 schemas) |
| `deploy/` | Docker, systemd, nginx configs for cloud VPS deployment (§10.2) |
| `config/` | YAML config for models, risk parameters, indices |
| `docs/` | PRD, architecture notes, API rate budget, design tokens |

## Non-negotiables baked into this scaffold

- **Paper mode is the default** everywhere — Live mode requires explicit activation (§4.5.5).
- **No tab/module fetches its own data** — everything reads from `engine/shared_state` (§7.3b).
- All SL/TSL/circuit-breaker parameters are **config-driven, never hardcoded** (§4.8.6).
- Fyers official docs: https://myapi.fyers.in/docsv3 — always verify current rate limits,
  STT %, and endpoint signatures there before changing `engine/data_layer/fyers_client`.
