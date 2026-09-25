# Desktop thin client (Tauri)

Per PRD §10.1/§10.3: this is a THIN CLIENT only. It does not run signal
logic locally — it connects to the cloud engine's dashboard/API. If your
laptop sleeps or loses wifi, the cloud engine keeps enforcing SL/TSL and
circuit breakers regardless.

Build: `cd desktop_client && npm install && npm run tauri build`
(requires Rust + Tauri CLI — see https://tauri.app/start/prerequisites/)
