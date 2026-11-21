//! Microphone capture for checks 5 and 6 (PRD §7.1 "cpal 0.18.2, opened on key-down and closed
//! on key-up"; PLAN.md M1 1a "cpal capture opened on key-down").
//!
//! The data callback never allocates or locks: it writes timestamps and level aggregates into
//! preallocated atomics. Samples are used for an RMS level and, in check 6, copied into the speech
//! library; they are never stored, logged or sent (PRD §15.1, CLAUDE.md "never record audio").

use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use m1_spike_common::clock::host_now_ns;
use m1_spike_common::stats::{Level, Summary, dbfs, summarize};
use serde::Serialize;

use crate::speech::Pusher;

/// Callbacks whose timestamps are kept (about 80 s at 10 ms buffers).
pub const MAX_CALLBACKS: usize = 8192;
/// Largest mono chunk handed to the speech library per push.
const MONO_CHUNK: usize = 4096;

/// Lock-free per-trial capture statistics, written by the audio callback.
#[derive(Debug)]
pub struct CaptureStats {
    callbacks: AtomicU64,
    frames: AtomicU64,
    min_frames: AtomicU64,
    max_frames: AtomicU64,
    errors: AtomicU64,
    last_error: Mutex<Option<String>>,
    cb_host_ns: Box<[AtomicU64]>,
    capture_lag_ns: Box<[AtomicU64]>,
    first_capture_ns: AtomicU64,
    sum_sq_bits: AtomicU64,
    peak_bits: AtomicU64,
    nonzero: AtomicU64,
    samples: AtomicU64,
    push_count: AtomicU64,
    push_frames: AtomicU64,
    push_max_ns: AtomicU64,
    push_total_ns: AtomicU64,
    push_over_1ms: AtomicU64,
    push_errors: AtomicU64,
}

impl Default for CaptureStats {
    fn default() -> Self {
        Self::new()
    }
}

impl CaptureStats {
    pub fn new() -> Self {
        let zeros = || {
            (0..MAX_CALLBACKS)
                .map(|_| AtomicU64::new(0))
                .collect::<Vec<_>>()
                .into_boxed_slice()
        };
        Self {
            callbacks: AtomicU64::new(0),
            frames: AtomicU64::new(0),
            min_frames: AtomicU64::new(u64::MAX),
            max_frames: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            last_error: Mutex::new(None),
            cb_host_ns: zeros(),
            capture_lag_ns: zeros(),
            first_capture_ns: AtomicU64::new(0),
            sum_sq_bits: AtomicU64::new(0f64.to_bits()),
            peak_bits: AtomicU64::new(0),
            nonzero: AtomicU64::new(0),
            samples: AtomicU64::new(0),
            push_count: AtomicU64::new(0),
            push_frames: AtomicU64::new(0),
            push_max_ns: AtomicU64::new(0),
            push_total_ns: AtomicU64::new(0),
            push_over_1ms: AtomicU64::new(0),
            push_errors: AtomicU64::new(0),
        }
    }

    /// Called once per buffer from the audio callback (single writer).
    fn record_buffer(&self, now_ns: u64, capture_ns: u64, frames: u64, level: &Level) {
        let i = self.callbacks.fetch_add(1, Ordering::Relaxed);
        if let Some(slot) = self.cb_host_ns.get(i as usize) {
            slot.store(now_ns, Ordering::Relaxed);
        }
        if let Some(slot) = self.capture_lag_ns.get(i as usize) {
            slot.store(now_ns.saturating_sub(capture_ns), Ordering::Relaxed);
        }
        if i == 0 {
            self.first_capture_ns.store(capture_ns, Ordering::Relaxed);
        }
        self.frames.fetch_add(frames, Ordering::Relaxed);
        self.min_frames.fetch_min(frames, Ordering::Relaxed);
        self.max_frames.fetch_max(frames, Ordering::Relaxed);
        self.sum_sq_bits
            .store(level.sum_squares.to_bits(), Ordering::Relaxed);
        self.peak_bits
            .store(u64::from(level.peak.to_bits()), Ordering::Relaxed);
        self.nonzero.store(level.nonzero, Ordering::Relaxed);
        self.samples.store(level.samples, Ordering::Relaxed);
    }

    fn record_push(&self, frames: usize, dt_ns: u64, rc: i32) {
        self.push_count.fetch_add(1, Ordering::Relaxed);
        self.push_frames.fetch_add(frames as u64, Ordering::Relaxed);
        self.push_total_ns.fetch_add(dt_ns, Ordering::Relaxed);
        self.push_max_ns.fetch_max(dt_ns, Ordering::Relaxed);
        if dt_ns > 1_000_000 {
            self.push_over_1ms.fetch_add(1, Ordering::Relaxed);
        }
        if rc != 0 {
            self.push_errors.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn record_error(&self, e: String) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut g) = self.last_error.lock() {
            *g = Some(e);
        }
    }

    /// Summarises the trial; call after the stream has been dropped.
    pub fn summary(&self) -> CaptureSummary {
        let n = self.callbacks.load(Ordering::Relaxed);
        let kept = (n as usize).min(MAX_CALLBACKS);
        let times: Vec<u64> = self.cb_host_ns[..kept]
            .iter()
            .map(|a| a.load(Ordering::Relaxed))
            .collect();
        let lags: Vec<f64> = self.capture_lag_ns[..kept]
            .iter()
            .map(|a| a.load(Ordering::Relaxed) as f64 / 1e6)
            .collect();
        let samples = self.samples.load(Ordering::Relaxed);
        let sum_sq = f64::from_bits(self.sum_sq_bits.load(Ordering::Relaxed));
        let rms = if samples == 0 {
            0.0
        } else {
            (sum_sq / samples as f64).sqrt()
        };
        let pushes = self.push_count.load(Ordering::Relaxed);
        CaptureSummary {
            callbacks: n,
            frames: self.frames.load(Ordering::Relaxed),
            frames_per_callback_min: (n > 0).then(|| self.min_frames.load(Ordering::Relaxed)),
            frames_per_callback_max: (n > 0).then(|| self.max_frames.load(Ordering::Relaxed)),
            first_callback_host_ns: times.first().copied(),
            last_callback_host_ns: times.last().copied(),
            first_capture_ns: (n > 0).then(|| self.first_capture_ns.load(Ordering::Relaxed)),
            interval_ms: summarize(&m1_spike_common::stats::intervals_ms(&times)),
            capture_to_callback_ms: summarize(&lags),
            rms_dbfs: dbfs(rms),
            peak: f32::from_bits(self.peak_bits.load(Ordering::Relaxed) as u32),
            nonzero_samples: self.nonzero.load(Ordering::Relaxed),
            samples,
            errors: self.errors.load(Ordering::Relaxed),
            last_error: self.last_error.lock().ok().and_then(|g| g.clone()),
            push: (pushes > 0).then(|| PushSummary {
                pushes,
                frames: self.push_frames.load(Ordering::Relaxed),
                max_us: self.push_max_ns.load(Ordering::Relaxed) as f64 / 1e3,
                mean_us: self.push_total_ns.load(Ordering::Relaxed) as f64 / 1e3 / pushes as f64,
                over_1ms: self.push_over_1ms.load(Ordering::Relaxed),
                errors: self.push_errors.load(Ordering::Relaxed),
            }),
        }
    }
}

/// How long `sg_speech_push_f32` blocked the audio thread (the ABI allows about 1 ms).
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct PushSummary {
    pub pushes: u64,
    pub frames: u64,
    pub max_us: f64,
    pub mean_us: f64,
    pub over_1ms: u64,
    pub errors: u64,
}

/// One trial's capture statistics.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CaptureSummary {
    pub callbacks: u64,
    pub frames: u64,
    pub frames_per_callback_min: Option<u64>,
    pub frames_per_callback_max: Option<u64>,
    pub first_callback_host_ns: Option<u64>,
    pub last_callback_host_ns: Option<u64>,
    /// cpal's capture timestamp of the first buffer (CoreAudio mHostTime, host-clock ns).
    pub first_capture_ns: Option<u64>,
    /// Callback-to-callback intervals (the buffer cadence).
    pub interval_ms: Option<Summary>,
    /// Callback arrival minus the buffer's capture timestamp.
    pub capture_to_callback_ms: Option<Summary>,
    pub rms_dbfs: f64,
    pub peak: f32,
    pub nonzero_samples: u64,
    pub samples: u64,
    pub errors: u64,
    pub last_error: Option<String>,
    pub push: Option<PushSummary>,
}

/// The default input as opened. The device name is reduced to "built-in or not": Bluetooth
/// device names often carry a person's name.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct InputInfo {
    pub sample_rate: u32,
    pub channels: u16,
    pub sample_format: String,
    pub builtin_mic: Option<bool>,
}

/// Finds the default input device and its default configuration.
pub fn default_input() -> Result<(cpal::Device, cpal::StreamConfig, InputInfo), String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "no default input device".to_string())?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("default_input_config: {e}"))?;
    let builtin_mic = device.description().ok().map(|d| {
        let n = d.name().to_ascii_lowercase();
        n.contains("macbook") || n.contains("built-in")
    });
    let info = InputInfo {
        sample_rate: supported.sample_rate(),
        channels: supported.channels(),
        sample_format: format!("{:?}", supported.sample_format()),
        builtin_mic,
    };
    Ok((device, supported.config(), info))
}

/// Builds and starts an f32 input stream (CoreAudio converts from the device format). When
/// `push` is given, each buffer is mixed to mono and pushed to the speech library in chunks.
pub fn open_stream(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    stats: Arc<CaptureStats>,
    push: Option<Pusher>,
) -> Result<cpal::Stream, String> {
    let channels = usize::from(config.channels.max(1));
    let cb_stats = stats.clone();
    let mut level = Level::default();
    let mut mono: Vec<f32> = Vec::with_capacity(MONO_CHUNK);
    let data_cb = move |data: &[f32], info: &cpal::InputCallbackInfo| {
        let now = host_now_ns();
        let capture_ns = u64::try_from(info.timestamp().capture.as_nanos()).unwrap_or(0);
        level.add(data);
        cb_stats.record_buffer(now, capture_ns, (data.len() / channels) as u64, &level);
        if let Some(p) = push {
            for chunk in data.chunks(MONO_CHUNK * channels) {
                mono.clear();
                for frame in chunk.chunks_exact(channels) {
                    mono.push(frame.iter().sum::<f32>() / channels as f32);
                }
                let t0 = host_now_ns();
                let rc = p.push(&mono);
                cb_stats.record_push(mono.len(), host_now_ns().saturating_sub(t0), rc);
            }
        }
    };
    let err_stats = stats;
    let err_cb = move |e: cpal::Error| err_stats.record_error(e.to_string());
    let stream = device
        .build_input_stream::<f32, _, _>(config, data_cb, err_cb, None)
        .map_err(|e| format!("build_input_stream: {e}"))?;
    stream.play().map_err(|e| format!("play: {e}"))?;
    Ok(stream)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summary_reports_cadence_level_and_pushes() {
        let s = CaptureStats::new();
        let mut level = Level::default();
        for k in 0..5u64 {
            level.add(&[0.5, -0.5, 0.0, 0.0]);
            s.record_buffer(
                1_000_000_000 + k * 10_000_000,
                995_000_000 + k * 10_000_000,
                480,
                &level,
            );
            s.record_push(480, 50_000, 0);
        }
        s.record_push(480, 2_000_000, 1);
        let sum = s.summary();
        assert_eq!(sum.callbacks, 5);
        assert_eq!(
            (
                sum.frames,
                sum.frames_per_callback_min,
                sum.frames_per_callback_max
            ),
            (2400, Some(480), Some(480))
        );
        let iv = sum.interval_ms.expect("intervals");
        assert!((iv.p50 - 10.0).abs() < 1e-9);
        let lag = sum.capture_to_callback_ms.expect("lag");
        assert!((lag.max - 5.0).abs() < 1e-9);
        assert_eq!(sum.nonzero_samples, 10);
        assert!((sum.rms_dbfs - (-9.0309)).abs() < 1e-3, "{}", sum.rms_dbfs);
        let p = sum.push.expect("push");
        assert_eq!((p.pushes, p.over_1ms, p.errors), (6, 1, 1));
        assert!((p.max_us - 2000.0).abs() < 1e-9);
    }

    #[test]
    fn an_idle_trial_has_no_buffers() {
        let sum = CaptureStats::new().summary();
        assert_eq!(sum.callbacks, 0);
        assert!(sum.first_callback_host_ns.is_none() && sum.interval_ms.is_none());
        assert_eq!(sum.rms_dbfs, -200.0);
        assert!(sum.push.is_none());
    }
}
