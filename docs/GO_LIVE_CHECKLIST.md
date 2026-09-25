# Go-Live Checklist (PRD §2, §5)

Do not switch to Live or Both mode until every item below is checked.
`engine/execution/mode_switcher.py` enforces `go_live_gates_cleared`
programmatically — this checklist is what that flag should be computed from.

- [ ] Expectancy per trade (net of costs) > 0, bootstrap CI excludes zero
- [ ] ≥ 500 trades OR 12 months of backtest history, whichever is larger
- [ ] Backtest window spans ≥ 1 high-VIX regime and ≥ 1 low-VIX grind regime
- [ ] Walk-forward validated (not just in-sample) across all standalone + ensemble models
- [ ] Max drawdown ≤ configured cap (`config/risk_params.yaml: weekly_drawdown_cap_pct`)
- [ ] Cost model (`config/cost_model.yaml`) verified against current Fyers/exchange rates
- [ ] Phase 2 (Paper) has run 4-8 weeks with live paper trades matching backtest expectancy within tolerance (§5)
- [ ] Manual kill switch tested end-to-end (`engine/risk_engine/kill_switch.py`)
- [ ] Broker/API failover + reconnect-and-reconcile procedure tested (§7.4 gap)
- [ ] Order rejection handling tested (margin shortfall, circuit limits, illiquid strike)
- [ ] Notification channel configured and alert delivery tested (§7.4 gap)
- [ ] Regulatory/compliance check done if going beyond your own capital (§11.2)
