// Tauri entrypoint — thin client shell around the frontend, per PRD §10.3.
// No trading logic here; all state comes from the cloud engine's API/WS.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Project Trade desktop shell");
}
