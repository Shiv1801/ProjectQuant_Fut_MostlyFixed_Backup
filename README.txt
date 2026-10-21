# OFI production integration patch

This ZIP contains replacement source files, using the repository-relative paths shown in the archive:

- `api/main.py`
- `engine/confluence_gate/quant_score.py`
- `engine/confluence_gate/gate.py`
- `engine/signal_layer/momentum/ofi.py`

## What changed

1. Spot OFI now receives explicit source timestamp/freshness metadata; missing/invalid/stale timestamps do not vote.
2. The live confluence evaluation now passes both spot and futures OFI to the confluence gate, and `require_ofi` can be enabled in the existing risk parameters to require either source.
3. A valid fresh one-contract futures OFI result (`partial`) can contribute, rather than requiring both monthly contracts to be valid.
4. Spot and futures are treated as two observations of the same OFI factor: when both are valid, each receives half the existing OFI vote weight, avoiding double-counting the factor. If only one is valid, it receives the existing full OFI weight.
5. The OFI calculation module is included unchanged from the uploaded current source for completeness.

## Install carefully

1. Keep live execution disabled and close the app before replacing files.
2. Extract the ZIP into the repository root, preserving the folder structure, and review/compare each replacement before accepting it.
3. Do not overwrite your database, credentials, settings, or other files.
4. Run `python -m py_compile api/main.py engine/confluence_gate/quant_score.py engine/confluence_gate/gate.py engine/signal_layer/momentum/ofi.py`.
5. Run your existing OFI/confluence tests offline. Do not connect to the live broker for testing.
6. Only after code review and tests should you restart the app in paper mode and inspect `/signals/ofi/NIFTY50`, `/signals/ofi/NIFTY50/futures`, and `/signals/confluence/NIFTY50`.

## Limitations

- This patch was prepared from uploaded file copies, not applied to your local working tree.
- The patch does not fetch spot order-book depth itself. It consumes the spot depth already placed in shared state by the existing poller.
- A stale/missing source timestamp makes spot OFI ineligible, by design.
- This patch does not verify live-market provider behavior, and it does not establish that the running process has loaded these files.
- Do not enable live trading based only on this ZIP; verify tests and paper-mode behavior first.
