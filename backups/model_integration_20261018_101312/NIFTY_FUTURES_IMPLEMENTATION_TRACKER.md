# NIFTY Futures Implementation Tracker

Last updated: 2026-10-18 (live validation completed; model-integration change requires strategy approval)

## 1. Objective and Scope

Build a reliable data pipeline for the nearest valid monthly NIFTY futures contract and the following valid monthly contract.

Included:

- Symbol-master discovery using actual Fyers `symTicker` and `expiryDate` values.
- Current/next contract selection and expiry rollover.
- Fyers quotes, one-minute OHLCV candles, market depth where available.
- Per-contract shared state, persistence, timestamps, freshness, and errors.
- Explicit unavailable/stale status after discovery or API failures.
- Duplicate futures-poller prevention without disrupting unrelated background tasks.
- Mocked unit tests and relevant regression tests.

Explicitly excluded:

- Connecting futures OFI to models, confluence, auto-entry, or execution.
- Changes to formulas, weights, thresholds, risk, sizing, stops, or order execution.
- Credentials, package installation, Git commit/push/history changes.
- Changes to backup implementations unless proven active.

Safety dependencies:

- Source files must be backed up before modification.
- `data/project_trade.db` must be backed up using SQLite backup before schema changes.
- Existing database data must not be deleted, reset, or rewritten.

## 2. Implementation Plan

Status: Approved by user on 2026-10-15. No implementation edits made yet at tracker creation.

| Order | Priority | Task | Affected files | Acceptance criteria |
|---|---|---|---|---|
| 1 | High | Confirm active startup and call chain | `api/main.py`, `engine/background/pollers.py` | Only active futures starter identified; unrelated tasks preserved |
| 2 | High | Back up source files and SQLite database | Affected source files; `data/project_trade.db` | Backups exist and database backup opens successfully |
| 3 | High | Implement current/next contract discovery | `symbol_master.py`, `ofi.py`, compatibility exports as needed | Actual expiry selects nearest two valid monthly NIFTY futures; expired/invalid data unavailable |
| 4 | High | Implement futures quote/candle/depth ingestion | `rest_client.py`, `pollers.py` | Both contracts remain isolated, validated, freshness/error status is explicit |
| 5 | High | Add per-contract shared state | `engine/shared_state/store.py` | Quote/candle/depth/discovery metadata available per contract |
| 6 | High | Add additive futures persistence | `config/db.py` | Contract symbol/expiry/timestamps preserved; duplicate writes prevented |
| 7 | High | Expose diagnostic status only | `api/main.py` | Diagnostic endpoint reports current/next and freshness; no model/trading wiring |
| 8 | High | Add mocked tests | `tests/unit/` and/or focused new test files | Discovery, rollover, failure, isolation, persistence, duplicate startup covered |
| 9 | High | Run tests and review diff | Repository | Focused and regression tests executed; tracker records actual results |

## 3. File-by-File Change Register

| Path | Purpose / existing behavior | Planned modification | Actual modification | Backup | Status |
|---|---|---|---|---|---|
| `engine/data_layer/fyers_client/symbol_master.py` | Downloads Fyers FO master and returns nearest two unexpired monthly contracts | Add explicit current/next selection and discovery status | Existing fetch path now receives role-labeled selections from discovery | `backups/nifty_futures_implementation_20261015/symbol_master.py` | Tested |
| `engine/signal_layer/momentum/ofi.py` | Filters monthly NIFTY symbols and computes diagnostic OFI | Extend discovery metadata only; leave OFI formula unchanged | Added current/next role labels; formula unchanged | `backups/nifty_futures_implementation_20261015/ofi.py` | Tested |
| `engine/signal_layer/momentum/futures_ofi.py` | Compatibility exports | Update only if discovery exports change | No change required | `backups/nifty_futures_implementation_20261015/futures_ofi.py` | Confirmed |
| `engine/data_layer/fyers_client/rest_client.py` | Fyers quotes, history, depth wrappers | Reuse or minimally extend wrappers for futures data | Added optional `cont_flag`; futures history uses non-continuous flag | `backups/nifty_futures_implementation_20261015/rest_client.py` | Tested |
| `engine/data_layer/futures_pipeline.py` | New pure futures validation/normalization helpers | Add quote, candle, depth normalization | Added mocked-testable helpers | N/A (new file) | Tested |
| `engine/background/pollers.py` | Active futures depth poller and task starters | Add quote/candle/depth ingestion and idempotent futures startup | Replaced diagnostic-only loop with current/next pipeline; added idempotent starter | `backups/nifty_futures_implementation_20261015/pollers.py` | Tested |
| `engine/shared_state/store.py` | In-memory spot/options/depth/futures diagnostic state | Add per-contract quote/candle/depth/status state | Added per-role data/status, rollover reset, and explicit discovery-unavailable handling | `backups/nifty_futures_implementation_20261015/store.py` | Tested |
| `config/db.py` | Existing SQLite schema and spot persistence | Additive futures tables and persistence/read helpers | Added futures quotes/candles/depth tables and deduplicated writers/readers | `backups/nifty_futures_implementation_20261015/db.py` | Tested on temporary DB; live DB schema not initialized |
| `api/main.py` | Active startup and futures diagnostic endpoint | Expose expanded diagnostic status only | Added discovery, per-contract data status, latest quote/candle diagnostics | `backups/nifty_futures_implementation_20261015/main.py` | Syntax-tested; endpoint not live-verified |
| `tests/unit/test_futures_pipeline.py` | New mocked futures pipeline tests | Cover discovery, rollover, validation, isolation, persistence, startup | Added 20 tests covering approved pipeline scope | N/A (new file) | Tested |
| `scripts/backfill_nifty_futures.py` | New current/next futures history backfill command | Download and persist one-minute history in 90-day chunks | Added and executed; FYERS returned `-99 Bad request` for all ten chunks | N/A (new file) | Blocked |
| `docs/NIFTY_FUTURES_IMPLEMENTATION_TRACKER.md` | Required project tracker | Maintain throughout work | Created with approved plan and backup record | N/A | Updated |

Backup verification completed before schema/source edits:

- Source backups: `backups/nifty_futures_implementation_20261015/`.
- SQLite backup: `backups/nifty_futures_implementation_20261015/project_trade.db`.
- Verification command: Python `sqlite3.Connection.backup()` followed by `PRAGMA integrity_check`.
- Result: `ok`.

## 4. Completed and Confirmed Changes

Pre-edit inspection confirmed:

- Active startup: `api/main.py:147-168` calls `init_db()`, then `start_background_pollers()` at line 150.
- Active futures starter: `engine/background/pollers.py:669-680`, with `futures_depth_poller()` scheduled at line 676.
- Existing futures discovery: `symbol_master.py:16-39` calls `discover_nifty_monthly_futures()`.
- Existing selector: `ofi.py:162-219` filters `symTicker`, uses `expiryDate`, removes expired records, sorts, and returns two.
- Existing futures state: `store.py:50-55,103-124` stores contracts and depth in memory only.
- Existing database: `config/db.py:49-146` contains no futures-specific tables.
- Existing duplicate unrelated task: candle enrichment is started from `pollers.py:678` and again from `api/main.py:161`.
- No active futures poller duplicate was found in the current startup chain.

Implementation status is code-updated, focused tests pass, and the running app initialized the additive futures schema in `data/project_trade.db`. Live Fyers delivery and live futures persistence remain unconfirmed because the market is closed. No model or trading logic was changed.

Official FYERS documentation check:

- `https://myapi.fyers.in/docsv3` was requested and returned HTTP 404.
- `https://myapi.fyers.in` loaded the official API documentation application shell.
- The official application bundle references `/docsv3` and an OpenAPI asset, but both the route and referenced v3 asset returned 404 during this session.
- The installed official SDK source confirms the existing methods and request parameters: `fyersModel.py:1219-1247` (`history`), `1249-1270` (`quotes`), `1272-1292` (`depth`), and `1295-1319` (`optionchain`).
- SDK-confirmed request conventions used by the existing wrappers: quotes accepts comma-separated `symbols`; history accepts `symbol`, `resolution`, `date_format`, `range_from`, `range_to`, and `cont_flag`; depth accepts `symbol` and `ohlcv_flag`.
- Official FYERS v3.1 rate-limit documentation was located at `https://myapi.fyers.in/static/media/v3.1.32a8eeba1fba866d1201.yaml`; it states non-transactional limits of 10/sec, 200/min, and 100,000/day. The active project comments also document a stricter Quotes endpoint limit and will not be weakened without direct endpoint evidence.
- Because the primary rendered v3 sections and response schemas were unavailable, response parsing will remain based on the existing active wrappers plus mocked/project-observed schemas; no undocumented endpoint will be introduced. This limitation remains open for live verification.

## 5. Pending Work and Blockers

- Review the complete diff and approve the pipeline stage: High priority.
- Live FYERS verification of quote/history/depth delivery: Medium priority, blocked until market hours.
- Live futures-row persistence/freshness verification: Medium priority, blocked until market hours; schema initialization is confirmed.
- Future model-provenance and model-integration stage: blocked pending separate approval.
- FYERS market-data requests: High priority blocker; current authenticated process returned `-99 Bad request` for quote, depth, and history requests for both futures and `NSE:NIFTY50-INDEX`.
- Automatic historical loading: High priority; current poller only fetches one-minute history during market hours, and the separate backfill command must succeed before historical futures rows can appear.

Models, confluence, risk, sizing, auto-entry, and execution must remain untouched.

## 6. Test and Validation Log

### Static inspection

| Date/time | Command/purpose | Result |
|---|---|---|
| 2026-10-15 | Repository search for startup and futures call sites | Passed; active chain identified; no active duplicate futures starter found |

### Unit tests

| Date/time | Command | Result |
|---|---|---|
| 2026-10-15 | `python -m pytest tests/unit/test_futures_pipeline.py -q` | Failed: 1 test expectation incorrectly required `ValueError` for a malformed dictionary; 8 passed, 1 failed. Test corrected to assert safe empty selection. |
| 2026-10-15 | `python -m pytest tests/unit/test_futures_pipeline.py test_ofi.py -q` | Failed during collection in pre-existing `tests/unit/test_kelly_sizing.py` when the broader `tests/unit` path was included in the command; `FractionalKelly` is not exported by the current module. This is unrelated to futures code. |
| 2026-10-15 | `python -m pytest tests/unit/test_futures_pipeline.py test_ofi.py -q` | Passed: 19 tests and 13 subtests. |
| 2026-10-15 | `python -m pytest tests/unit/test_futures_pipeline.py test_ofi.py -q` | Failed after rollover-state reset was added: one test expected pre-reset metadata equality; implementation behavior was correct and test was updated. |
| 2026-10-15 | `python -m pytest tests/unit/test_futures_pipeline.py test_ofi.py -q` | Passed: 20 tests and 13 subtests after OHLC validation, ordering, and deduplication coverage was added. |

### Regression tests

| 2026-10-15 | `python -m pytest tests/unit/test_trailing_stop.py tests/unit/test_stop_loss.py tests/unit/test_confluence_gate.py -q` | Failed: 7 passed, 2 failed. Existing `test_confluence_gate.py` calls an incompatible `evaluate_confluence()` signature; no futures files were involved. |
| 2026-10-15 | `python -m pytest tests/integration/test_shared_state_roundtrip.py -q` | Failed during collection because existing `config.settings.Settings` has no `redis_url`; unrelated to futures changes and no Redis integration was run. |

### Integration/live-market tests

| 2026-10-15 21:08 IST | Read-only `Invoke-RestMethod http://127.0.0.1:8001/auth/status` | Passed: server returned `status=ok`, `has_token=true`, `is_stale=false`. |
| 2026-10-15 21:08 IST | Read-only `Invoke-RestMethod http://127.0.0.1:8001/pollers/status` | Inconclusive after-hours: app and pollers reported running, but futures discovery had not run because local time was 21:08 IST, outside configured 09:00-15:40 market hours. |
| 2026-10-15 21:08 IST | Read-only `Invoke-RestMethod http://127.0.0.1:8001/signals/ofi/NIFTY50/futures` | Inconclusive after-hours: `status=unavailable`, zero contracts discovered, no discovery error. This is expected before the market-hours discovery branch executes. |
| 2026-10-15 21:08 IST | Read-only SQLite row counts for `futures_quotes`, `futures_candles`, `futures_depth_snapshots` | All three tables exist and contain zero rows; no live market session has populated them yet. |
| 2026-10-15 21:13 IST | Read-only running-app schema inspection on `data/project_trade.db` | Passed: all three futures tables exist with contract symbol, role, expiry, timestamps, status, and uniqueness indexes; all currently contain zero rows. |
| 2026-10-15 21:13 IST | Read-only live `PRAGMA integrity_check` | Not completed: the running application held the SQLite database busy beyond the 30-second read-only command timeout. The pre-edit backup integrity check remains `ok`; no live integrity result is claimed. |
| 2026-10-15 21:15 IST | `git diff --check` and changed-file review | Passed with line-ending warnings only. No model, confluence, risk, position-sizing, execution, or auto-entry Python files changed. |
| 2026-10-15 21:15 IST | `python -m pytest tests/unit/test_futures_pipeline.py test_ofi.py -q` | Passed: 20 tests and 13 subtests. |
| 2026-10-15 21:15 IST | Read-only `Invoke-RestMethod http://127.0.0.1:8001/auth/status` | Passed: server status `ok`, token present, token not stale. |
| 2026-10-15 21:40 IST | `python scripts/backfill_nifty_futures.py 365` | Blocked: symbol master discovered current/next symbols, but all ten history chunks returned FYERS `{'s': 'error', 'code': -99, 'message': 'Bad request'}`; zero rows inserted. |
| 2026-10-15 21:41 IST | Direct Fyers `quotes`, `depth`, and `history` calls for current futures and `NSE:NIFTY50-INDEX` | Blocked: each returned `code=-99`, `message=Bad request`; no data was persisted by these checks. |
| 2026-10-15 22:05 IST | Read-only restarted-app `/health`, `/pollers/status`, and `/signals/ofi/NIFTY50/futures` | Restart confirmed (`server_started_at=2026-10-15T16:33:27`); symbol discovery selected `NSE:NIFTY26OCTFUT` and `NSE:NIFTY26NOVFUT`; data fields are explicitly `market_closed` and no futures rows exist. |
| 2026-10-15 22:05 IST | Read-only `/fyers/profile` | Passed: FYERS authenticated profile returned `s=ok`, `code=200`; credentials/token are accepted for profile access. |
| 2026-10-15 22:05 IST | Read-only database counts | Existing spot candles: 228,621 rows; futures quotes/candles/depth: zero rows. |
| 2026-10-15 22:16 IST | `python scripts/backfill_nifty_futures.py 1` | Attempted today's current/next futures history; both symbols returned FYERS `-99 Bad request`; current rows inserted: 0, next rows inserted: 0, failures: 2. |
| 2026-10-15 22:16 IST | Read-only futures table counts after today's attempt | Quotes: 0, candles: 0, depth: 0; no data was persisted. |
| 2026-10-15 22:18 IST | Direct current-month history attempt for `NSE:NIFTY26OCTFUT` on `2026-10-15` | FYERS returned `-99 Bad request`; valid candles: 0; inserted: 0; futures candle count remains 0. |
| 2026-10-16 09:11 IST | Continuous futures chart/API implementation | Added expiry-date selection across persisted contracts, additive back-adjustment at rollover, `/candles/NIFTY50` continuous output, and a Lightweight Charts consumer. Raw per-contract candles remain unchanged. |
| 2026-10-16 09:11 IST | Rollover regression and backend syntax checks | Passed: `21` tests plus `13` subtests; `py_compile` passed. Frontend build not run because `frontend/node_modules` is absent (`vite` not recognized). |
| 2026-10-16 10:30 IST | Live refresh/model-health implementation | UI now uses the real `/ws/prices` socket, refreshes chart data and model health every 1 second, displays all A-G models, overlays the current futures quote on the forming candle, and formats chart ticks in Asia/Kolkata time. Added `/models/live/{index_id}` and `/market/futures`. |
| 2026-10-16 10:30 IST | Runtime verification after restart | Prices, option chain, spot depth, futures poller, decision loop, and futures OFI were healthy. Numeric-string Fyers quote timestamps are now accepted and both futures quotes were valid. Spot OFI returned `no_depth`; correlation breakdown is intentionally not applicable for the NIFTY-only app. |
| 2026-10-16 10:30 IST | Regression verification | Passed: `22` tests plus `13` subtests; backend `py_compile` passed. Frontend build remains unverified because `frontend/node_modules` is absent. |

## 7. Data-Source and Model-Provenance Matrix

| Consumer/data | Source | Symbol | Timeframe/freshness | Status |
|---|---|---|---|---|
| Spot price models | Fyers `quotes()` | `NSE:NIFTY50-INDEX` | Poller approximately 3 seconds | Existing and active |
| Spot candles/VWAP | Fyers `history()` plus live ticks | `NSE:NIFTY50-INDEX` | One-minute candles; volume enrichment near minute boundary | Existing and active |
| Spot OFI | Fyers `depth()` | `NSE:NIFTY50-INDEX` | Poller approximately 2 seconds | Existing and active |
| Current NIFTY futures | Fyers FO symbol master, then quotes/history/depth | Resolved `symTicker` | Actual `expiryDate`; per-contract statuses and timestamps | Symbol discovery works; quote/history/depth blocked by live `-99` response |
| Next NIFTY futures | Fyers FO symbol master, then quotes/history/depth | Resolved `symTicker` | Following actual valid expiry; per-contract statuses and timestamps | Symbol discovery works; quote/history/depth blocked by live `-99` response |
| Options | Fyers `optionchain()` | Provider-returned symbols | Poller approximately 2 seconds | Existing and active |
| India VIX | Fyers `quotes()` and live candle path | `NSE:INDIAVIX-INDEX` | Existing spot poller cadence | Existing and active |
| Futures OFI | Shared futures depth -> `calculate_futures_ofi()` | Current/next resolved symbols | Existing diagnostic freshness 15 seconds | Diagnostic-only; no trading integration |

## 8. Official FYERS Documentation References and Verified Methods

| Area | Official source | Verified evidence / limitation |
|---|---|---|
| API v3 documentation | `https://myapi.fyers.in/docsv3` | Official route, but returned HTTP 404 during this session |
| Official API app shell | `https://myapi.fyers.in` | Loaded official documentation app; JavaScript references `/docsv3` |
| Quotes | Installed `fyers_apiv3/fyersModel.py:1249-1270` | `quotes(data)` accepts `symbols` as comma-separated symbols; official rendered response schema unavailable |
| History | Installed `fyers_apiv3/fyersModel.py:1219-1247` | `history(data)` documents `symbol`, resolutions including `1`, date format, ranges, and `cont_flag`; response schema unavailable in rendered docs |
| Market depth | Installed `fyers_apiv3/fyersModel.py:1272-1292` | `depth(data)` documents `symbol` and `ohlcv_flag`; response schema unavailable in rendered docs |
| Option chain | Installed `fyers_apiv3/fyersModel.py:1295-1319` | Existing unrelated option-chain wrapper confirmed; no changes planned |
| Rate limits | `https://myapi.fyers.in/static/media/v3.1.32a8eeba1fba866d1201.yaml` | Official v3.1 document states non-transactional 10/sec, 200/min, 100,000/day |
| Symbol master | No accessible rendered v3 section; active project URL `https://public.fyers.in/sym_details/NSE_FO_sym_master.json` | Existing source uses the public Fyers symbol master; exact rendered schema documentation was unavailable |

No credentials, authentication procedures, or permissions were changed.

## 9. Risks, Decisions, and Rollback

Decisions:

- Current means the nearest valid monthly contract by actual expiry.
- Next means the following valid monthly contract by actual expiry.
- Discovery failure must not present stale/expired contracts as live.
- Futures data will remain diagnostic-only.
- Persistence will use additive SQLite changes only.

Risks:

- Fyers response schemas may omit quote timestamps or depth for a contract; status must then be explicit rather than fabricated.
- FYERS currently returns `-99 Bad request` for tested authenticated market-data calls, including spot; root cause is unresolved and must be diagnosed before further integration.
- Symbol-master refresh failure can occur during rollover; the last data may be retained only as diagnostic stale data.
- SQLite schema changes must preserve all existing trading data.

Rollback:

- Restore backed-up source files individually, never with a destructive Git reset.
- If needed, restore the SQLite database backup only after stopping the application and preserving the affected database copy; no automatic database restore is authorized.

## 10. Chronological Change History

| Date/time | Change |
|---|---|
| 2026-10-15 | Approved implementation plan recorded in tracker. |
| 2026-10-15 | Active startup chain inspected; one active futures starter confirmed, unrelated candle-enrichment duplication identified. |
| 2026-10-15 | Source backups created under `backups/nifty_futures_implementation_20261015/`. |
| 2026-10-15 | SQLite backup created with `sqlite3.Connection.backup()` and verified with `PRAGMA integrity_check`: `ok`. |
| 2026-10-15 | Added role labels to futures discovery output in `ofi.py`; no OFI formula changed. |
| 2026-10-15 | Added uncommitted pure futures normalization module `engine/data_layer/futures_pipeline.py`. Tests and integration wiring pending. |
| 2026-10-15 | Official v3 docs route returned 404; official SDK method signatures and official v3.1 rate-limit asset were recorded. |
| 2026-10-15 | Wired quotes, non-continuous one-minute history, depth, shared state, additive persistence, diagnostics, and idempotent futures startup. |
| 2026-10-15 | Focused pipeline tests passed: 20 tests and 13 subtests. `py_compile` passed for all changed Python files. |
| 2026-10-15 | Existing regression failures preserved: Kelly test collection, confluence gate signature mismatch, and Redis settings integration collection error. |
| 2026-10-15 | Added canonical current-futures candle reads for NIFTY technical analysis, moved futures symbol discovery before the market-hours gate, and added `scripts/backfill_nifty_futures.py`. |
| 2026-10-15 | Backfill and direct FYERS verification blocked by repeated `-99 Bad request`; no futures rows were inserted. |

## 11. Next Session Handover

Current stage: Stage D blocked at live provider verification; stopped before model integration.

Last completed task: implemented and tested the contract-aware futures data pipeline and diagnostics.

Next task: diagnose the FYERS `-99 Bad request` response during an approved/live-valid API session, then rerun backfill and freshness verification. Do not proceed to model integration until successful futures data is persisted.

Outstanding approval: none for the approved scope.

Do not modify model, confluence, risk, sizing, auto-entry, or execution files.

## 12. 2026-10-18 Validation and Handover

### Verified

- Restored `config/db.py` after the disk-full interruption; import and backend startup now succeed.
- Live `/health` returned `status=ok`; server startup timestamp was `2026-10-18T09:42:20+05:30`.
- Live `/market/futures` selected `NSE:NIFTY26OCTFUT` and `NSE:NIFTY26NOVFUT`; after-hours status is explicit as `market_closed`.
- Live `/candles/count/all` reported `770` persisted futures candles; latest persisted candle was `2026-10-16T10:09:00+00:00` and is rendered in IST by the UI.
- Live `/candles/NIFTY50` returned continuous candles with `source=fyers_history` and preserved contract provenance.
- Live `/signals/correlation-breakdown` returned `not_applicable`; this remains correct because the app has only the NIFTY50 return series.
- Live `/models/live/NIFTY50` returned production evaluation IDs and explicit integration statuses for each model.
- Focused regression command passed: `28 passed`.
- Backend `py_compile` passed for all changed Python files.
- `git diff --check` passed with only expected LF/CRLF warnings.

### Strategy Integration Blocker

The current live endpoint confirms that several models are intentionally not quant-score contributors, including GARCH, IV Rank, variance risk premium, variance ratio, VIX/RV-IV context, futures OFI, and correlation breakdown. Making these models score contributors or changing directional trade eligibility would change approved scoring/trading behavior. No such strategy change was made.

Before implementation, the user must approve the exact production specification for:

- Per-model score mapping and weights.
- Gate versus score versus timing versus sizing semantics.
- Directional model aggregation and the exact `NEUTRAL` behavior.
- Whether futures OFI is allowed to influence confluence after live-data validation.
- Whether correlation remains disabled until a second approved index is added.

Until that specification is approved, existing confluence, directional, risk, sizing, auto-entry, and execution behavior remains unchanged.
