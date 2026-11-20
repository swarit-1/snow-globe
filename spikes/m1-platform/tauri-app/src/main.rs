//! `m1-spike`: the throwaway Tauri 2 app behind PLAN.md M1 1a checks 1-7, plus the `tags` mode
//! that feeds the ScreenCaptureKit checks (8, 10, 11) run by `m1-sck`.
//! PRD §5.4 (controls and focus model), §6 (Tauri 2 shell, Swift static library, cpal),
//! §10.2-10.6 (reveal on key-down, measurement, trace, replay harness).
//!
//! The owner runs it (README.md); building and the unit tests need no permission, window,
//! microphone or network.

mod audio;
mod checks;
mod config;
mod macos;
mod netwatch;
mod panel;
mod speech;
mod verdicts;

use std::process::ExitCode;
use std::sync::Arc;

use m1_spike_common::jsonl::RunLog;
use tauri::{AppHandle, Manager, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent};

use crate::checks::Spike;
use crate::config::{Config, USAGE};
use crate::panel::{PANEL_LABEL, PANEL_W};

fn on_shortcut(app: &AppHandle, shortcut: &Shortcut, event: ShortcutEvent) {
    if let Some(s) = app.try_state::<Arc<Spike>>() {
        let stamp = s.log.stamp();
        s.inner().on_shortcut(stamp, shortcut, event.state);
    }
}

/// Everything the page reports (see ui/spike.js). Sync command: runs on the main thread.
#[tauri::command]
fn js_event(app: AppHandle, ev: serde_json::Value) {
    if let Some(s) = app.try_state::<Arc<Spike>>() {
        let stamp = s.log.stamp();
        s.inner().on_js(stamp, ev);
    }
}

fn setup(app: &mut tauri::App, cfg: Config, log: Arc<RunLog>) -> Result<(), String> {
    // No Dock icon and no menu-bar takeover; with activate_ignoring_other_apps(false) the app
    // does not take focus from the terminal at launch either.
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    let handle = app.handle().clone();
    let hotkey: Shortcut = cfg
        .hotkey
        .parse()
        .map_err(|e| format!("{}: {e}", cfg.hotkey))?;
    let esc: Shortcut = "Escape".parse().map_err(|e| format!("Escape: {e}"))?;
    let throttling = panel::throttling_from(&cfg.throttling)?;
    let level = panel::level_from(&cfg.level)?;
    let x = handle
        .primary_monitor()
        .ok()
        .flatten()
        .map(|m| f64::from(m.size().width) / m.scale_factor() - PANEL_W - 40.0)
        .unwrap_or(60.0)
        .max(0.0);
    let panel =
        panel::build(&handle, throttling, level, x, 60.0).map_err(|e| format!("panel: {e}"))?;
    let spike = Arc::new(Spike::new(handle.clone(), cfg, log, hotkey, esc, panel));
    app.manage(spike.clone());
    if let Some(w) = handle.get_webview_window(PANEL_LABEL) {
        let s = spike.clone();
        w.on_window_event(move |e| {
            if let WindowEvent::Focused(f) = e {
                let stamp = s.log.stamp();
                s.on_window_focus(stamp, *f);
            }
        });
    }
    if let Err(e) = handle.global_shortcut().register(hotkey) {
        let msg = format!(
            "could not register {} as a global shortcut: {e}",
            spike.cfg.hotkey
        );
        spike
            .log
            .ev("hotkey_register_failed", serde_json::json!({"error": msg}));
        return Err(msg);
    }
    spike.start();
    Ok(())
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let cfg = match Config::parse(&argv) {
        Ok(Some(c)) => c,
        Ok(None) => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Err(e) => {
            eprintln!("m1-spike: {e}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let log = match RunLog::open(&cfg.run_id, cfg.check.slug(), "m1-spike", &argv) {
        Ok(l) => Arc::new(l),
        Err(e) => {
            eprintln!("m1-spike: cannot open the run log: {e}");
            return ExitCode::from(2);
        }
    };
    let app = tauri::Builder::default()
        .activate_ignoring_other_apps(false)
        .plugin(tauri_nspanel::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(on_shortcut)
                .build(),
        )
        .invoke_handler(tauri::generate_handler![js_event])
        .setup(move |app| setup(app, cfg, log).map_err(Into::into))
        .build(tauri::generate_context!());
    match app {
        Ok(app) => {
            app.run(|_, _| {});
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("m1-spike: {e}");
            ExitCode::from(1)
        }
    }
}
