//! The spike's overlay panel (PRD §5.4, §6.5: `tauri-nspanel` 2.1.1, a non-activating,
//! always-on-top NSPanel; PLAN.md M1 1a checks 2-4 and 8).
//!
//! The panel is a Tauri webview window converted to an NSPanel subclass that can become key
//! (for click-to-key focus) but carries `NSWindowStyleMaskNonactivatingPanel`, so showing or
//! clicking it never activates the app. It is created hidden, borderless, without a shadow, on
//! all Spaces and beside full-screen apps.
#![allow(missing_debug_implementations)]

use tauri::utils::config::BackgroundThrottlingPolicy;
use tauri::{AppHandle, LogicalPosition, LogicalSize, Position, Size, WebviewUrl, Wry};
use tauri_nspanel::{CollectionBehavior, PanelBuilder, PanelHandle, PanelLevel, StyleMask};

pub const PANEL_LABEL: &str = "spike-panel";
/// The title `m1-sck` looks for.
pub const PANEL_TITLE: &str = "Snow Globe M1 spike panel";
pub const PANEL_W: f64 = 480.0;
pub const PANEL_H: f64 = 300.0;

tauri_nspanel::tauri_panel! {
    panel!(SnowGlobeSpikePanel {
        config: {
            can_become_key_window: true,
            is_floating_panel: true
        }
    })
}

/// Window level choices for `--level`.
pub fn level_from(name: &str) -> Result<PanelLevel, String> {
    match name {
        "floating" => Ok(PanelLevel::Floating),
        "status" => Ok(PanelLevel::Status),
        "popup" => Ok(PanelLevel::PopUpMenu),
        other => Err(format!("--level {other:?}: use floating, status or popup")),
    }
}

/// Background-throttling choices for `--throttling` (Tauri maps them to WKWebView's
/// `inactiveSchedulingPolicy` on macOS 14+). `None` keeps WebKit's default (suspend).
pub fn throttling_from(name: &str) -> Result<Option<BackgroundThrottlingPolicy>, String> {
    match name {
        "default" => Ok(None),
        "suspend" => Ok(Some(BackgroundThrottlingPolicy::Suspend)),
        "throttle" => Ok(Some(BackgroundThrottlingPolicy::Throttle)),
        "disabled" => Ok(Some(BackgroundThrottlingPolicy::Disabled)),
        other => Err(format!(
            "--throttling {other:?}: use default, suspend, throttle or disabled"
        )),
    }
}

/// Builds the hidden panel at `(x, y)` logical points from the primary screen's top-left.
pub fn build(
    app: &AppHandle,
    throttling: Option<BackgroundThrottlingPolicy>,
    level: PanelLevel,
    x: f64,
    y: f64,
) -> tauri::Result<PanelHandle<Wry>> {
    let panel = PanelBuilder::<_, SnowGlobeSpikePanel<Wry>>::new(app, PANEL_LABEL)
        .url(WebviewUrl::App("index.html".into()))
        .title(PANEL_TITLE)
        .size(Size::Logical(LogicalSize::new(PANEL_W, PANEL_H)))
        .position(Position::Logical(LogicalPosition::new(x, y)))
        .level(level)
        .floating(true)
        .has_shadow(false)
        .hides_on_deactivate(false)
        .becomes_key_only_if_needed(false)
        .no_activate(true)
        .add_style_mask(StyleMask::empty().nonactivating_panel())
        .collection_behavior(
            CollectionBehavior::new()
                .can_join_all_spaces()
                .full_screen_auxiliary()
                .stationary(),
        )
        .with_window(move |w| {
            let w = w
                .decorations(false)
                .visible(false)
                .focused(false)
                .resizable(false)
                .skip_taskbar(true)
                .accept_first_mouse(true);
            match throttling {
                Some(p) => w.background_throttling(p),
                None => w,
            }
        })
        .build()?;
    Ok(panel)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_level_and_throttling_options() {
        assert_eq!(level_from("status").map(|l| l.value()), Ok(25));
        assert_eq!(level_from("floating").map(|l| l.value()), Ok(4));
        assert!(level_from("top").is_err());
        assert_eq!(throttling_from("default"), Ok(None));
        assert_eq!(
            throttling_from("disabled"),
            Ok(Some(BackgroundThrottlingPolicy::Disabled))
        );
        assert!(throttling_from("off").is_err());
    }
}
