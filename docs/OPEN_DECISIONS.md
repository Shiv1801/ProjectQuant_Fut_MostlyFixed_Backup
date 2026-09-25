# Open Decisions Requiring Your Input (PRD §12)

1. "Pure math, no indicators" boundary — confirm Hurst, GARCH, VRP in-scope.
2. Backtest window — 6 months (original spec) vs 24 months (recommended). **This scaffold assumes 24mo** (`scripts/run_backtest.py --window 24m`).
3. Retrain cadence — literal daily vs weekly-batch. **This scaffold assumes weekly-batch** (`deploy/systemd/project-trade-retrain.timer`).
4. Instrument scope v1 — pure long calls/puts only, or debit spreads too? **This scaffold assumes calls/puts only.**
5. Capital priority — rigid waterfall vs expectancy-ranked with index tiebreaker. **Not yet implemented — see `engine/execution/order_manager` TODO.**
6. Option chain poll interval — RESOLVED: 1 sec flat, uniform across all 3 indices.
7. Notification channel — **this scaffold wires Telegram by default**, extend `engine/notifications/` for SMS/push.
8. Manual kill switch — **implemented**: `engine/risk_engine/kill_switch.py`, `api/routers/mode_control.py`.
9. .exe framework — **this scaffold uses Tauri** (`desktop_client/`) per PRD recommendation (lighter than Electron).
10. Cloud region — **this scaffold assumes AWS ap-south-1 / Mumbai-based VPS**, not enforced in code.
11. Subscription sequencing — deferred; see `migrations/002_multi_tenant_placeholder.sql`.

Update this file as decisions get made — several config defaults above
encode an assumed answer and should be revisited.
