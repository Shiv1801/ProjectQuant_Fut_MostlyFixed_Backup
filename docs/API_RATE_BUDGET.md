# Fyers API Rate Budget (PRD §7.3)

Always verify current limits against https://myapi.fyers.in/docsv3 before
relying on the numbers below — they change.

| Data need | Method | Frequency | Approx calls/day |
|---|---|---|---|
| Tick data (all 3 indices) | WebSocket | continuous | 0 (not REST) |
| Option chain full refresh | REST poll | 1 sec, uniform across all 3 indices | ~51,840/day |
| Historical backfill | REST | on-demand | ~1,000/day reserve |
| Order placement/modification | REST | trade-triggered | ~300-500/day |
| Account/funds/positions sync | REST | every 30-60s | ~400-750/day |
| **Total** | | | **~54,000/day** (~46% headroom vs 100k cap) |

Design decision: option chain polls at 1 second **uniformly across all 3
indices**, not just the actively-viewed one — a signal can fire on any
index regardless of what's on-screen (PRD §7.3).
