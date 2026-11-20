//! Host clock helpers (PRD §10.4 "Clocks"; PLAN.md M1 1a check 10).
//!
//! The paint harness joins timestamps from two processes: the app stamps key-up with Rust
//! `Instant`, and the ScreenCaptureKit helper gets frame times from Core Media's host clock. Both
//! must be the same clock for "key-up to composited paint" to mean anything. On macOS the host
//! clock is `mach_absolute_time`, which `CLOCK_UPTIME_RAW` exposes in nanoseconds and which stops
//! while the machine sleeps. Every spike record carries `t_host_ns` from this clock, so records
//! from different processes can be put on one timeline.

use std::time::{Duration, Instant};

use serde::Serialize;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct MachTimebaseInfo {
    numer: u32,
    denom: u32,
}

// SAFETY: these are libSystem functions with C signatures matching these declarations
// (<time.h> clock_gettime_nsec_np, <mach/mach_time.h> mach_absolute_time, mach_continuous_time,
// mach_timebase_info), available since macOS 10.12.
unsafe extern "C" {
    fn clock_gettime_nsec_np(clock_id: libc::clockid_t) -> u64;
    fn mach_absolute_time() -> u64;
    fn mach_continuous_time() -> u64;
    fn mach_timebase_info(info: *mut MachTimebaseInfo) -> libc::c_int;
}

/// Nanoseconds on the host clock (`CLOCK_UPTIME_RAW`, the same clock as `mach_absolute_time`,
/// paused during sleep). This is the cross-process timeline for every spike record.
pub fn host_now_ns() -> u64 {
    // SAFETY: clock_gettime_nsec_np takes a clock id by value and has no other preconditions.
    unsafe { clock_gettime_nsec_np(libc::CLOCK_UPTIME_RAW) }
}

/// Nanoseconds on `CLOCK_MONOTONIC_RAW`, which keeps counting during sleep. Used only to tell the
/// two clocks apart when checking which one `Instant` reads.
pub fn monotonic_raw_now_ns() -> u64 {
    // SAFETY: as for host_now_ns.
    unsafe { clock_gettime_nsec_np(libc::CLOCK_MONOTONIC_RAW) }
}

/// Raw `mach_absolute_time` ticks (host time units; convert with [`Timebase`]).
pub fn mach_now_ticks() -> u64 {
    // SAFETY: mach_absolute_time has no preconditions.
    unsafe { mach_absolute_time() }
}

/// Raw `mach_continuous_time` ticks (counts sleep), for diagnostics only.
pub fn mach_continuous_now_ticks() -> u64 {
    // SAFETY: mach_continuous_time has no preconditions.
    unsafe { mach_continuous_time() }
}

/// The ratio that turns mach ticks into nanoseconds (`ns = ticks * numer / denom`).
/// Apple silicon reports 125/3 (24 MHz ticks); Intel reports 1/1.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Timebase {
    pub numer: u32,
    pub denom: u32,
}

impl Timebase {
    /// Reads the timebase from the kernel. Falls back to 1/1 if the call reports a zero divisor.
    pub fn current() -> Self {
        let mut info = MachTimebaseInfo::default();
        // SAFETY: `info` is a valid, writable MachTimebaseInfo with the C layout mach expects.
        let rc = unsafe { mach_timebase_info(&mut info) };
        if rc != 0 || info.denom == 0 || info.numer == 0 {
            return Self { numer: 1, denom: 1 };
        }
        Self {
            numer: info.numer,
            denom: info.denom,
        }
    }

    /// Converts mach ticks to nanoseconds without overflow.
    pub fn ticks_to_ns(self, ticks: u64) -> u64 {
        let ns = u128::from(ticks) * u128::from(self.numer) / u128::from(self.denom.max(1));
        u64::try_from(ns).unwrap_or(u64::MAX)
    }

    /// Converts nanoseconds to mach ticks without overflow.
    pub fn ns_to_ticks(self, ns: u64) -> u64 {
        let ticks = u128::from(ns) * u128::from(self.denom) / u128::from(self.numer.max(1));
        u64::try_from(ticks).unwrap_or(u64::MAX)
    }
}

/// One timestamp in both forms every record carries: monotonic nanoseconds since the process's
/// clock origin (from `Instant`), and host nanoseconds (from `CLOCK_UPTIME_RAW`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Stamp {
    pub mono_ns: u64,
    pub host_ns: u64,
}

/// A per-process clock origin. `stamp()` reads `Instant` and the host clock back to back.
#[derive(Clone, Copy, Debug)]
pub struct Clock {
    origin: Instant,
    origin_host_ns: u64,
}

impl Clock {
    /// Starts a clock at "now".
    pub fn start() -> Self {
        let origin = Instant::now();
        let origin_host_ns = host_now_ns();
        Self {
            origin,
            origin_host_ns,
        }
    }

    /// Reads both clocks now.
    pub fn stamp(&self) -> Stamp {
        let now = Instant::now();
        let host_ns = host_now_ns();
        Stamp {
            mono_ns: duration_ns(now.saturating_duration_since(self.origin)),
            host_ns,
        }
    }

    /// Converts an `Instant` taken earlier in this process to a stamp, mapping it onto the host
    /// clock through the origin. Valid because `Instant` is `CLOCK_UPTIME_RAW` on macOS (checked
    /// by [`instant_identity`] and by check 10).
    pub fn stamp_of(&self, instant: Instant) -> Stamp {
        let mono_ns = duration_ns(instant.saturating_duration_since(self.origin));
        Stamp {
            mono_ns,
            host_ns: self.origin_host_ns.saturating_add(mono_ns),
        }
    }

    /// Host nanoseconds at this clock's origin.
    pub fn origin_host_ns(&self) -> u64 {
        self.origin_host_ns
    }

    /// The `Instant` this clock started at.
    pub fn origin(&self) -> Instant {
        self.origin
    }
}

/// Converts a `Duration` to whole nanoseconds, saturating at `u64::MAX`.
pub fn duration_ns(d: Duration) -> u64 {
    u64::try_from(d.as_nanos()).unwrap_or(u64::MAX)
}

/// Converts a Core Media time (value / timescale seconds) to nanoseconds. Returns `None` for a
/// non-positive timescale or a result outside `i64`.
pub fn cmtime_to_ns(value: i64, timescale: i32) -> Option<i64> {
    if timescale <= 0 {
        return None;
    }
    let ns = i128::from(value) * 1_000_000_000 / i128::from(timescale);
    i64::try_from(ns).ok()
}

/// One paired read of `Instant` and the host clock: the host clock was read immediately before
/// (`host_before_ns`) and after (`host_after_ns`) the `Instant`, which was `instant_ns` after the
/// first sample's `Instant`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PairedRead {
    pub instant_ns: u64,
    pub host_before_ns: u64,
    pub host_after_ns: u64,
}

/// How closely `Instant` tracked the host clock over a series of paired reads.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct InstantAgreement {
    pub samples: usize,
    /// Host-clock span covered by the samples, in milliseconds.
    pub span_ms: f64,
    /// Offset of `Instant` from the host clock (relative to the first sample), min and max.
    pub offset_min_ns: i64,
    pub offset_max_ns: i64,
    /// Offset change from the first to the last sample.
    pub drift_ns: i64,
    /// Widest host-clock bracket around one `Instant` read (the measurement's own uncertainty).
    pub max_bracket_ns: u64,
}

/// Computes [`InstantAgreement`] from paired reads. Pure; returns `None` for fewer than two reads.
pub fn agreement_from(reads: &[PairedRead]) -> Option<InstantAgreement> {
    let first = reads.first()?;
    if reads.len() < 2 {
        return None;
    }
    let mid0 = midpoint(first.host_before_ns, first.host_after_ns);
    let mut offsets = Vec::with_capacity(reads.len());
    let mut max_bracket_ns = 0u64;
    for r in reads {
        let mid = midpoint(r.host_before_ns, r.host_after_ns);
        let host_elapsed = i128::from(mid) - i128::from(mid0);
        let inst_elapsed = i128::from(r.instant_ns) - i128::from(first.instant_ns);
        let off = i64::try_from(inst_elapsed - host_elapsed).unwrap_or(i64::MAX);
        offsets.push(off);
        max_bracket_ns = max_bracket_ns.max(r.host_after_ns.saturating_sub(r.host_before_ns));
    }
    let last = reads.last()?;
    let span_ns = midpoint(last.host_before_ns, last.host_after_ns).saturating_sub(mid0);
    Some(InstantAgreement {
        samples: reads.len(),
        span_ms: span_ns as f64 / 1e6,
        offset_min_ns: offsets.iter().copied().min().unwrap_or(0),
        offset_max_ns: offsets.iter().copied().max().unwrap_or(0),
        drift_ns: offsets.last().copied().unwrap_or(0) - offsets.first().copied().unwrap_or(0),
        max_bracket_ns,
    })
}

fn midpoint(a: u64, b: u64) -> u64 {
    a / 2 + b / 2 + (a % 2 + b % 2) / 2
}

/// Takes `samples` paired reads of `Instant` and the host clock, `spacing` apart, and reports
/// their agreement. This is a real measurement (it sleeps for `samples * spacing`).
pub fn measure_instant_agreement(samples: usize, spacing: Duration) -> Option<InstantAgreement> {
    let origin = Instant::now();
    let mut reads = Vec::with_capacity(samples);
    for k in 0..samples {
        if k > 0 {
            std::thread::sleep(spacing);
        }
        let host_before_ns = host_now_ns();
        let now = Instant::now();
        let host_after_ns = host_now_ns();
        reads.push(PairedRead {
            instant_ns: duration_ns(now.saturating_duration_since(origin)),
            host_before_ns,
            host_after_ns,
        });
    }
    agreement_from(&reads)
}

/// Parses the `Debug` form of a Unix `Instant` (`Instant { tv_sec: 12, tv_nsec: 34 }`) into
/// nanoseconds. The format is not a stable API, so this returns `None` rather than guessing.
pub fn parse_instant_debug(s: &str) -> Option<u64> {
    let sec = number_after(s, "tv_sec:")?;
    let nsec = number_after(s, "tv_nsec:")?;
    if nsec >= 1_000_000_000 {
        return None;
    }
    sec.checked_mul(1_000_000_000)?.checked_add(nsec)
}

fn number_after(s: &str, key: &str) -> Option<u64> {
    let start = s.find(key)? + key.len();
    let digits: String = s[start..]
        .trim_start()
        .chars()
        .take_while(char::is_ascii_digit)
        .collect();
    digits.parse().ok()
}

/// Which system clock `Instant` reads, found by comparing its absolute value with
/// `CLOCK_UPTIME_RAW` and `CLOCK_MONOTONIC_RAW`. The two differ by the time the machine has slept
/// since boot, so the answer is only conclusive after the machine has slept at least once.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct InstantIdentity {
    pub instant_ns: Option<u64>,
    pub uptime_raw_ns: u64,
    pub monotonic_raw_ns: u64,
    /// Time slept since boot (monotonic-raw minus uptime-raw).
    pub slept_since_boot_ns: i64,
    pub diff_vs_uptime_raw_ns: Option<i64>,
    pub diff_vs_monotonic_raw_ns: Option<i64>,
    /// "uptime_raw", "monotonic_raw", "indistinguishable", "other" or "unknown".
    pub verdict: String,
}

/// Reads `Instant`, `CLOCK_UPTIME_RAW` and `CLOCK_MONOTONIC_RAW` back to back and classifies
/// `Instant` with a 1 ms tolerance (see [`classify_identity`]).
pub fn instant_identity() -> InstantIdentity {
    let debug = format!("{:?}", Instant::now());
    let uptime_raw_ns = host_now_ns();
    let monotonic_raw_ns = monotonic_raw_now_ns();
    classify_identity(parse_instant_debug(&debug), uptime_raw_ns, monotonic_raw_ns)
}

/// Pure classification behind [`instant_identity`].
pub fn classify_identity(
    instant_ns: Option<u64>,
    uptime_raw_ns: u64,
    monotonic_raw_ns: u64,
) -> InstantIdentity {
    const TOL_NS: i64 = 1_000_000;
    let slept = signed_diff(monotonic_raw_ns, uptime_raw_ns);
    let du = instant_ns.map(|i| signed_diff(i, uptime_raw_ns));
    let dm = instant_ns.map(|i| signed_diff(i, monotonic_raw_ns));
    let verdict = match (du, dm) {
        (Some(u), Some(m)) => {
            let near_u = u.abs() < TOL_NS;
            let near_m = m.abs() < TOL_NS;
            match (near_u, near_m) {
                (true, true) => "indistinguishable",
                (true, false) => "uptime_raw",
                (false, true) => "monotonic_raw",
                (false, false) => "other",
            }
        }
        _ => "unknown",
    };
    InstantIdentity {
        instant_ns,
        uptime_raw_ns,
        monotonic_raw_ns,
        slept_since_boot_ns: slept,
        diff_vs_uptime_raw_ns: du,
        diff_vs_monotonic_raw_ns: dm,
        verdict: verdict.to_string(),
    }
}

fn signed_diff(a: u64, b: u64) -> i64 {
    i64::try_from(i128::from(a) - i128::from(b)).unwrap_or(i64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apple_silicon_timebase_converts_24mhz_ticks() {
        let tb = Timebase {
            numer: 125,
            denom: 3,
        };
        assert_eq!(tb.ticks_to_ns(24_000_000), 1_000_000_000);
        assert_eq!(tb.ns_to_ticks(1_000_000_000), 24_000_000);
        // Large values do not overflow.
        assert_eq!(tb.ticks_to_ns(u64::MAX / 2), u64::MAX);
    }

    #[test]
    fn cmtime_conversion_handles_scales_and_bad_input() {
        assert_eq!(cmtime_to_ns(3, 1), Some(3_000_000_000));
        assert_eq!(cmtime_to_ns(1_500, 1_000), Some(1_500_000_000));
        assert_eq!(cmtime_to_ns(24_000_000, 24_000_000), Some(1_000_000_000));
        assert_eq!(cmtime_to_ns(5, 0), None);
        assert_eq!(cmtime_to_ns(5, -1), None);
        assert_eq!(cmtime_to_ns(i64::MAX, 1), None);
    }

    #[test]
    fn agreement_detects_drift_and_bracket() {
        // Instant runs 2 µs fast over the series; brackets are 40 ns wide.
        let reads: Vec<PairedRead> = (0..5u64)
            .map(|k| PairedRead {
                instant_ns: k * 1_000_000 + k * 500,
                host_before_ns: 10_000 + k * 1_000_000 - 20,
                host_after_ns: 10_000 + k * 1_000_000 + 20,
            })
            .collect();
        let a = agreement_from(&reads).expect("two or more reads");
        assert_eq!(a.samples, 5);
        assert_eq!(a.drift_ns, 2_000);
        assert_eq!(a.offset_min_ns, 0);
        assert_eq!(a.offset_max_ns, 2_000);
        assert_eq!(a.max_bracket_ns, 40);
        assert!((a.span_ms - 4.0).abs() < 1e-9);
        assert!(agreement_from(&reads[..1]).is_none());
    }

    #[test]
    fn parses_unix_instant_debug_output() {
        assert_eq!(
            parse_instant_debug("Instant { tv_sec: 1372554, tv_nsec: 689416208 }"),
            Some(1_372_554_689_416_208)
        );
        assert_eq!(parse_instant_debug("Instant { t: 12 }"), None);
        assert_eq!(
            parse_instant_debug("Instant { tv_sec: 1, tv_nsec: 1000000000 }"),
            None
        );
    }

    #[test]
    fn classifies_instant_clock() {
        let slept = 2_000_000_000_000u64;
        let up = 5_000_000_000u64;
        let mono = up + slept;
        assert_eq!(
            classify_identity(Some(up + 50), up, mono).verdict,
            "uptime_raw"
        );
        assert_eq!(
            classify_identity(Some(mono - 50), up, mono).verdict,
            "monotonic_raw"
        );
        assert_eq!(
            classify_identity(Some(up), up, up + 10).verdict,
            "indistinguishable"
        );
        assert_eq!(classify_identity(None, up, mono).verdict, "unknown");
        assert_eq!(classify_identity(Some(1), up, mono).verdict, "other");
    }

    /// A real measurement on this machine, not a fixture: `Instant` must advance with the host
    /// clock, and (when the machine has slept since boot) read CLOCK_UPTIME_RAW itself. This is the
    /// `Instant` half of check 10; the ScreenCaptureKit half needs Screen Recording (`m1-sck`).
    #[test]
    fn instant_is_the_host_clock_on_this_machine() {
        let a = measure_instant_agreement(200, Duration::from_micros(500)).expect("reads");
        assert!(
            a.drift_ns.unsigned_abs() <= a.max_bracket_ns + 1_000,
            "Instant drifted from CLOCK_UPTIME_RAW: {a:?}"
        );
        let id = instant_identity();
        assert!(
            id.verdict == "uptime_raw" || id.verdict == "indistinguishable",
            "Instant is not CLOCK_UPTIME_RAW: {id:?}"
        );
        let tb = Timebase::current();
        let host = host_now_ns();
        let mach_ns = tb.ticks_to_ns(mach_now_ticks());
        assert!(
            mach_ns.abs_diff(host) < 1_000_000,
            "mach {mach_ns} vs host {host}"
        );
        eprintln!("instant agreement: {a:?}\ninstant identity: {id:?}\ntimebase: {tb:?}");
    }

    #[test]
    fn clock_stamps_are_consistent() {
        let c = Clock::start();
        let earlier = Instant::now();
        std::thread::sleep(Duration::from_millis(2));
        let s = c.stamp();
        assert!(s.mono_ns >= 2_000_000);
        assert!(s.host_ns >= c.origin_host_ns() + 2_000_000);
        let mapped = c.stamp_of(earlier);
        assert!(mapped.host_ns <= s.host_ns);
        assert!(mapped.host_ns >= c.origin_host_ns());
    }
}
