# Architecture Notes

This mirrors PRD §3 (System Architecture diagram) and §7.3b (single-source-of-truth).

## Data flow

```
Fyers WS (ticks, order updates) ─┐
Fyers REST (1-sec option chain)  ├─► engine/data_layer ─► engine/shared_state (Redis pub/sub)
                                  ┘                              │
                                                    ┌─────────────┴──────────────┐
                                                    ▼                             ▼
                                          engine/signal_layer            api/ (FastAPI reads)
                                          (Groups A-G, §3.5)                     │
                                                    │                             ▼
                                                    ▼                     frontend/ (6 tabs, WS push)
                                          engine/confluence_gate
                                          (§4.3.5 AND gate)
                                                    │
                                                    ▼
                                          engine/strike_selector (§4.4)
                                                    │
                                                    ▼
                                          engine/risk_engine (sizing, §4.8.1-.6)
                                                    │
                                                    ▼
                                          engine/execution (Paper/Live/Both, §4.5.5)
                                                    │
                                                    ▼
                                          engine/data_layer/storage (TimescaleDB, F1-F14)
```

## Non-negotiable rule (§7.3b)

Only `engine/data_layer` and `engine/shared_state/publisher.py` ever WRITE to
shared state. Every other module — signal layer, confluence gate, risk
engine, API routers, frontend — only READS from shared state or the DB.
This is what prevents tabs, calculations, and displayed values from
silently drifting out of sync.

## Module → PRD section map

| Module | PRD section |
|---|---|
| `engine/data_layer` | §4.1 |
| `engine/signal_layer` | §3.5 (A-G), §4.3 |
| `engine/confluence_gate` | §4.3.5 |
| `engine/strike_selector` | §4.4 |
| `engine/execution` | §4.5, §4.5.5 |
| `engine/risk_engine` | §4.8 (all subsections) |
| `engine/backtest` | §4.6 |
| `engine/ml_pipeline` | §4.7 |
| `engine/model_layer/rejection_detector.py` | §4.8.2b (Model E) |
| `api/` + `frontend/` | §7.2, §7.3b |
| `desktop_client/` | §10.1, §10.3 |
