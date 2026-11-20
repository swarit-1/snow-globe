//! AppKit observations for the focus checks (PLAN.md M1 1a checks 2 and 3; PRD §5.4 "The panel
//! is non-activating: showing it never takes keyboard focus from the app you are in").
//!
//! Everything here is read-only and needs no permission: the frontmost application from
//! NSWorkspace, whether this app is active, the panel's key/visible/occlusion state, and the
//! current modifier flags. Call [`snapshot`] on the main thread.

use objc2::MainThreadMarker;
use objc2_app_kit::{NSApplication, NSEvent, NSPanel, NSWindowOcclusionState, NSWorkspace};
use serde::Serialize;

use m1_spike_common::accel::Mods;
use m1_spike_common::clock::host_now_ns;

/// Who has focus, at one instant.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FocusSnapshot {
    pub host_ns: u64,
    pub frontmost_pid: Option<i32>,
    pub frontmost_bundle: Option<String>,
    pub frontmost_name: Option<String>,
    /// `NSApp.isActive` for the spike app (`None` off the main thread).
    pub app_active: Option<bool>,
    pub panel_visible: Option<bool>,
    pub panel_key: Option<bool>,
    /// The window server reports part of the panel visible on screen.
    pub panel_on_screen: Option<bool>,
    pub panel_level: Option<isize>,
}

/// Reads the focus state now. Must run on the main thread to read the app and panel state.
pub fn snapshot(panel: Option<&NSPanel>) -> FocusSnapshot {
    let host_ns = host_now_ns();
    let front = NSWorkspace::sharedWorkspace().frontmostApplication();
    let (frontmost_pid, frontmost_bundle, frontmost_name) = match &front {
        Some(app) => (
            Some(app.processIdentifier()),
            app.bundleIdentifier().map(|s| s.to_string()),
            app.localizedName().map(|s| s.to_string()),
        ),
        None => (None, None, None),
    };
    let mtm = MainThreadMarker::new();
    let app_active = mtm.map(|m| NSApplication::sharedApplication(m).isActive());
    let (panel_visible, panel_key, panel_on_screen, panel_level) = match (mtm, panel) {
        (Some(_), Some(p)) => (
            Some(p.isVisible()),
            Some(p.isKeyWindow()),
            Some(p.occlusionState().contains(NSWindowOcclusionState::Visible)),
            Some(p.level()),
        ),
        _ => (None, None, None, None),
    };
    FocusSnapshot {
        host_ns,
        frontmost_pid,
        frontmost_bundle,
        frontmost_name,
        app_active,
        panel_visible,
        panel_key,
        panel_on_screen,
        panel_level,
    }
}

/// `NSEvent.modifierFlags` (class property): the modifier keys held right now.
pub fn modifier_flags() -> u64 {
    NSEvent::modifierFlags_class().0 as u64
}

/// The NSEventModifierFlags bits for an accelerator's modifiers.
pub fn modifier_mask(m: &Mods) -> u64 {
    let mut mask = 0u64;
    if m.shift {
        mask |= 1 << 17;
    }
    if m.control {
        mask |= 1 << 18;
    }
    if m.option {
        mask |= 1 << 19;
    }
    if m.command {
        mask |= 1 << 20;
    }
    mask
}

/// True when every modifier in `mask` is held in `flags`.
pub fn modifiers_held(flags: u64, mask: u64) -> bool {
    flags & mask == mask
}

/// The panel's frame, scale and window number (for `m1-sck`).
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Geometry {
    pub window_number: isize,
    pub x_pt: f64,
    pub y_pt: f64,
    pub width_pt: f64,
    pub height_pt: f64,
    pub backing_scale: f64,
}

/// Reads the panel geometry (main thread).
pub fn geometry(panel: &NSPanel) -> Geometry {
    let f = panel.frame();
    Geometry {
        window_number: panel.windowNumber(),
        x_pt: f.origin.x,
        y_pt: f.origin.y,
        width_pt: f.size.width,
        height_pt: f.size.height,
        backing_scale: panel.backingScaleFactor(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_match_nseventmodifierflags() {
        let m = Mods {
            control: true,
            shift: true,
            option: false,
            command: false,
        };
        let mask = modifier_mask(&m);
        assert_eq!(mask, (1 << 17) | (1 << 18));
        assert!(modifiers_held(mask | (1 << 8), mask));
        assert!(!modifiers_held(1 << 18, mask));
    }
}
