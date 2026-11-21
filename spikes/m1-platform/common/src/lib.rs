//! Shared pieces of the M1 1a platform spike (PLAN.md M1 1a).
//!
//! Everything here is plain logic that needs no permission, window, microphone or network, so it
//! is unit-tested with `cargo test`. The checks that do need the platform live in the `tauri-app`,
//! `sck` and `inject` crates and only call into this crate for logging, timing maths and verdicts.
//!
//! - [`jsonl`]: the run log every check writes to `$SNOW_GLOBE_DATA_DIR/spikes/m1/<run-id>.jsonl`.
//! - [`clock`]: the host clock (CLOCK_UPTIME_RAW / `mach_absolute_time`) and how `Instant` maps to it.
//! - [`stats`]: percentiles, intervals and RMS.
//! - [`tag`]: the in-surface pixel tag that carries (query_gen, tier) (PRD §10.4).
//! - [`hotkey`]: press/release pairing for the hotkey and injection checks.
//! - [`accel`]: accelerator strings to macOS key codes and event flags, for `CGEventPost`.
//! - [`netwatch`]: the `nettop` parser behind the network-silence check.
//! - [`checks`]: the eleven PLAN items, their criteria and permissions.
//! - [`consent`]: check 11's verdict, from the helper's measurement and the owner's note.
//! - [`report`]: the offline report and the cross-process joins.

pub mod accel;
pub mod args;
pub mod checks;
pub mod clock;
pub mod consent;
pub mod hotkey;
pub mod jsonl;
pub mod netwatch;
pub mod paths;
pub mod report;
pub mod stats;
pub mod tag;
