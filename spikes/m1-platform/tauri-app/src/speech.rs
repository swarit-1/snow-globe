//! Bridge to app/speech-macos's Swift static library through its C ABI (`lookout_speech.h`;
//! PRD §6.5 "Swift in-process static library, C ABI", §7.1 engine A1 `DictationTranscriber`;
//! PLAN.md M1 1a check 6).
//!
//! The extern declarations are compiled only with `--features speech`, because the library is
//! built by another work package and may not exist yet. Without the feature,
//! [`SpeechSession::start`] returns an error and check 6 is recorded as skipped. The callback
//! trampoline and the event sink are always compiled, so they are unit-tested either way.
//!
//! Transcript text is never logged (PRD §10.5). The sink keeps only lengths and word counts, and
//! prints the text to the terminal only when the owner passes `--print-text`.
// Without the library the callback side is reachable only from the tests.
#![cfg_attr(not(feature = "speech"), allow(dead_code))]

use std::ffi::{CStr, c_char, c_void};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use m1_spike_common::clock::host_now_ns;
use serde::Serialize;

/// `lk_event_kind`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    Ready,
    Partial,
    Final,
    Error,
    Unknown,
}

impl EventKind {
    /// Maps the C enum value; values outside the header's list are `Unknown`.
    pub fn from_raw(k: u32) -> Self {
        match k {
            0 => Self::Ready,
            1 => Self::Partial,
            2 => Self::Final,
            3 => Self::Error,
            _ => Self::Unknown,
        }
    }
}

/// One callback from the library, without its text.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SpeechEvent {
    pub kind: EventKind,
    pub raw_kind: u32,
    pub host_ns: u64,
    pub text_len: usize,
    pub words: usize,
    pub audio_start_s: f64,
    pub audio_end_s: f64,
    pub error_code: i32,
}

/// Collects a session's callbacks (they arrive on the library's internal queue).
#[derive(Debug)]
pub struct SpeechSink {
    events: Mutex<Vec<SpeechEvent>>,
    cv: Condvar,
    print_text: bool,
    trial: usize,
}

impl SpeechSink {
    pub fn new(trial: usize, print_text: bool) -> Self {
        Self {
            events: Mutex::new(Vec::new()),
            cv: Condvar::new(),
            print_text,
            trial,
        }
    }

    /// Records one callback. `text` is read, measured and dropped.
    pub fn deliver(&self, raw_kind: u32, text: Option<&CStr>, a0: f64, a1: f64, err: i32) {
        let host_ns = host_now_ns();
        let kind = EventKind::from_raw(raw_kind);
        let s = text.map(|t| t.to_string_lossy());
        let text_len = s.as_ref().map_or(0, |t| t.chars().count());
        let words = s.as_ref().map_or(0, |t| t.split_whitespace().count());
        if self.print_text
            && let Some(t) = &s
        {
            println!("[speech trial {}] {kind:?}: {t}", self.trial);
        }
        let ev = SpeechEvent {
            kind,
            raw_kind,
            host_ns,
            text_len,
            words,
            audio_start_s: a0,
            audio_end_s: a1,
            error_code: err,
        };
        if let Ok(mut v) = self.events.lock() {
            v.push(ev);
        }
        self.cv.notify_all();
    }

    /// Waits until a FINAL or ERROR has arrived, or `timeout` passes. True if one arrived.
    pub fn wait_terminal(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let Ok(mut guard) = self.events.lock() else {
            return false;
        };
        loop {
            if guard
                .iter()
                .any(|e| matches!(e.kind, EventKind::Final | EventKind::Error))
            {
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            match self.cv.wait_timeout(guard, deadline - now) {
                Ok((g, _)) => guard = g,
                Err(_) => return false,
            }
        }
    }

    /// A copy of the events so far.
    pub fn events(&self) -> Vec<SpeechEvent> {
        self.events.lock().map(|v| v.clone()).unwrap_or_default()
    }
}

/// The `lk_speech_cb` registered with `lk_speech_start`. `ctx` is the `SpeechSink` pointer the
/// session passed in; it stays valid until `lk_speech_cancel` and `lk_speech_free` have returned.
///
/// # Safety
/// `ctx` must be null or point to a live `SpeechSink`, and `text` must be null or a valid
/// NUL-terminated string for the duration of the call (the header's contract).
pub unsafe extern "C" fn on_speech_event(
    ctx: *mut c_void,
    kind: u32,
    text: *const c_char,
    audio_start_s: f64,
    audio_end_s: f64,
    error_code: i32,
) {
    if ctx.is_null() {
        return;
    }
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        // SAFETY: the caller guarantees ctx points to a live SpeechSink (see the fn docs).
        let sink = unsafe { &*(ctx as *const SpeechSink) };
        let text = if text.is_null() {
            None
        } else {
            // SAFETY: non-null text is a valid C string for the duration of the callback.
            Some(unsafe { CStr::from_ptr(text) })
        };
        sink.deliver(kind, text, audio_start_s, audio_end_s, error_code);
    }));
}

/// What `lk_speech_status` codes mean (lookout_speech.h).
pub fn status_meaning(code: i32) -> &'static str {
    match code {
        0 => "ready",
        1 => "model asset not installed",
        2 => "not authorized",
        3 => "locale unsupported",
        c if c < 0 => "other error",
        _ => "undocumented code",
    }
}

#[cfg(feature = "speech")]
mod ffi {
    use std::ffi::{c_char, c_void};

    /// Opaque `lk_speech` session.
    #[repr(C)]
    #[derive(Debug)]
    pub struct LkSpeech {
        _private: [u8; 0],
    }

    /// `lk_speech_cb`. `lk_event_kind` is a C enum with values 0-3, passed as a 32-bit integer.
    pub type LkSpeechCb = unsafe extern "C" fn(*mut c_void, u32, *const c_char, f64, f64, i32);

    // SAFETY: these declarations mirror app/speech-macos/include/lookout_speech.h exactly
    // (checked textually by build.rs when the header exists).
    unsafe extern "C" {
        pub fn lk_speech_status(locale_bcp47: *const c_char) -> i32;
        pub fn lk_speech_start(
            locale_bcp47: *const c_char,
            sample_rate_hz: f64,
            cb: Option<LkSpeechCb>,
            ctx: *mut c_void,
        ) -> *mut LkSpeech;
        pub fn lk_speech_push_f32(s: *mut LkSpeech, mono: *const f32, frames: usize) -> i32;
        pub fn lk_speech_finish(s: *mut LkSpeech) -> i32;
        pub fn lk_speech_cancel(s: *mut LkSpeech);
        pub fn lk_speech_free(s: *mut LkSpeech);
        pub fn lk_speech_version() -> *const c_char;
    }
}

/// True when the binary was built with `--features speech`.
pub fn library_linked() -> bool {
    cfg!(feature = "speech")
}

/// `lk_speech_version()`, or `None` when the library is not linked.
pub fn version() -> Option<String> {
    #[cfg(feature = "speech")]
    {
        // SAFETY: lk_speech_version returns a static NUL-terminated string or null.
        let p = unsafe { ffi::lk_speech_version() };
        if p.is_null() {
            return Some(String::new());
        }
        // SAFETY: non-null, NUL-terminated per the header.
        Some(unsafe { CStr::from_ptr(p) }.to_string_lossy().into_owned())
    }
    #[cfg(not(feature = "speech"))]
    {
        None
    }
}

/// `lk_speech_status(locale)`.
pub fn status(locale: &str) -> Result<i32, String> {
    #[cfg(feature = "speech")]
    {
        let c = std::ffi::CString::new(locale).map_err(|e| e.to_string())?;
        // SAFETY: c is a valid NUL-terminated string for the call.
        Ok(unsafe { ffi::lk_speech_status(c.as_ptr()) })
    }
    #[cfg(not(feature = "speech"))]
    {
        let _ = locale;
        Err("built without --features speech".into())
    }
}

/// A copyable handle the audio callback uses to push samples.
#[derive(Clone, Copy, Debug)]
pub struct Pusher {
    raw: *mut c_void,
}

// SAFETY: lk_speech_push_f32 is designed to be called from the audio thread while the control
// thread owns the session (lookout_speech.h); the session outlives every push because the audio
// stream is dropped before lk_speech_finish/cancel/free.
unsafe impl Send for Pusher {}

impl Pusher {
    /// Pushes mono samples (the library copies them). Returns the library's status code.
    pub fn push(&self, mono: &[f32]) -> i32 {
        #[cfg(feature = "speech")]
        {
            // SAFETY: raw is a live session (see the Send impl); mono is valid for its length.
            unsafe { ffi::lk_speech_push_f32(self.raw.cast(), mono.as_ptr(), mono.len()) }
        }
        #[cfg(not(feature = "speech"))]
        {
            let _ = (mono, self.raw);
            -1
        }
    }
}

/// One `lk_speech` session and the sink its callbacks write to.
#[derive(Debug)]
pub struct SpeechSession {
    raw: *mut c_void,
    sink: *const SpeechSink,
    closed: bool,
}

// SAFETY: the session is created, finished and freed on the audio control thread; the raw
// pointers are only used through the library's thread-safe C ABI.
unsafe impl Send for SpeechSession {}

impl SpeechSession {
    /// `lk_speech_start(locale, sample_rate, on_speech_event, sink)`.
    pub fn start(locale: &str, sample_rate_hz: f64, sink: Arc<SpeechSink>) -> Result<Self, String> {
        #[cfg(feature = "speech")]
        {
            let c = std::ffi::CString::new(locale).map_err(|e| e.to_string())?;
            let ctx = Arc::into_raw(sink);
            // SAFETY: c is valid for the call; ctx points to a live SpeechSink that is released
            // only in close(), after lk_speech_cancel and lk_speech_free.
            let raw = unsafe {
                ffi::lk_speech_start(
                    c.as_ptr(),
                    sample_rate_hz,
                    Some(on_speech_event),
                    ctx as *mut c_void,
                )
            };
            if raw.is_null() {
                // SAFETY: ctx came from Arc::into_raw above and the library holds no reference.
                drop(unsafe { Arc::from_raw(ctx) });
                return Err("lk_speech_start returned NULL".into());
            }
            Ok(Self {
                raw: raw.cast(),
                sink: ctx,
                closed: false,
            })
        }
        #[cfg(not(feature = "speech"))]
        {
            let _ = (locale, sample_rate_hz, sink);
            Err("built without --features speech".into())
        }
    }

    pub fn pusher(&self) -> Pusher {
        Pusher { raw: self.raw }
    }

    /// `lk_speech_finish`: key-up; the library flushes and then sends exactly one FINAL or ERROR.
    pub fn finish(&self) -> i32 {
        #[cfg(feature = "speech")]
        {
            // SAFETY: raw is a live session until close().
            unsafe { ffi::lk_speech_finish(self.raw.cast()) }
        }
        #[cfg(not(feature = "speech"))]
        {
            -1
        }
    }

    /// `lk_speech_cancel` (no callback after it returns), then `lk_speech_free`, then releases the
    /// sink reference held for the callbacks.
    pub fn close(mut self) {
        self.close_inner();
    }

    fn close_inner(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        #[cfg(feature = "speech")]
        {
            // SAFETY: raw is a live session; after cancel returns no callback runs, so the sink
            // can be released after free.
            unsafe {
                ffi::lk_speech_cancel(self.raw.cast());
                ffi::lk_speech_free(self.raw.cast());
                drop(Arc::from_raw(self.sink));
            }
        }
        #[cfg(not(feature = "speech"))]
        {
            let _ = self.sink;
        }
    }
}

impl Drop for SpeechSession {
    fn drop(&mut self) {
        self.close_inner();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    #[test]
    fn trampoline_records_lengths_not_text() {
        let sink = Arc::new(SpeechSink::new(1, false));
        let ctx = Arc::as_ptr(&sink) as *mut c_void;
        let partial = CString::new("show me rainier").expect("cstring");
        // SAFETY: ctx points to a live sink; the strings outlive the calls.
        unsafe {
            on_speech_event(ctx, 0, std::ptr::null(), 0.0, 0.0, 0);
            on_speech_event(ctx, 1, partial.as_ptr(), 0.1, 1.2, 0);
            on_speech_event(ctx, 9, std::ptr::null(), 0.0, 0.0, 0);
            on_speech_event(std::ptr::null_mut(), 2, std::ptr::null(), 0.0, 0.0, 0);
        }
        let ev = sink.events();
        assert_eq!(ev.len(), 3);
        assert_eq!(ev[0].kind, EventKind::Ready);
        assert_eq!(
            (ev[1].kind, ev[1].text_len, ev[1].words),
            (EventKind::Partial, 15, 3)
        );
        assert_eq!(ev[2].kind, EventKind::Unknown);
        let json = serde_json::to_string(&ev).expect("json");
        assert!(
            !json.contains("rainier"),
            "transcript text leaked into the record: {json}"
        );
    }

    #[test]
    fn wait_terminal_returns_on_final_or_timeout() {
        let sink = Arc::new(SpeechSink::new(2, false));
        assert!(!sink.wait_terminal(Duration::from_millis(20)));
        let s2 = sink.clone();
        let h = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(30));
            s2.deliver(2, None, 0.0, 1.0, 0);
        });
        assert!(sink.wait_terminal(Duration::from_secs(2)));
        h.join().expect("join");
    }

    #[test]
    fn status_codes_are_named() {
        assert_eq!(status_meaning(0), "ready");
        assert_eq!(status_meaning(2), "not authorized");
        assert_eq!(status_meaning(-4), "other error");
    }

    #[cfg(not(feature = "speech"))]
    #[test]
    fn without_the_feature_the_session_reports_why() {
        let sink = Arc::new(SpeechSink::new(1, false));
        let e = SpeechSession::start("en-US", 48_000.0, sink).expect_err("no library");
        assert!(e.contains("--features speech"));
        assert!(!library_linked());
        assert!(version().is_none());
    }

    /// When the other work package's header exists, check that it declares exactly the ABI this
    /// bridge was written against (whitespace-insensitive). Skipped while the header is absent.
    #[test]
    fn header_matches_the_abi_when_present() {
        let header =
            m1_spike_common::paths::repo_root().join("app/speech-macos/include/lookout_speech.h");
        let Ok(h) = std::fs::read_to_string(&header) else {
            eprintln!("skipped: {} not written yet", header.display());
            return;
        };
        let squash = |s: &str| s.split_whitespace().collect::<String>();
        let h = squash(&h);
        for proto in [
            "int32_t lk_speech_status(const char *locale_bcp47);",
            "lk_speech *lk_speech_start(const char *locale_bcp47, double sample_rate_hz, lk_speech_cb cb, void *ctx);",
            "int32_t lk_speech_push_f32(lk_speech *s, const float *mono, size_t frames);",
            "int32_t lk_speech_finish(lk_speech *s);",
            "void lk_speech_cancel(lk_speech *s);",
            "void lk_speech_free(lk_speech *s);",
            "const char *lk_speech_version(void);",
            "typedef void (*lk_speech_cb)(void *ctx, lk_event_kind kind, const char *utf8_text, double audio_start_s, double audio_end_s, int32_t error_code);",
            "LK_EV_READY = 0",
            "LK_EV_PARTIAL = 1",
            "LK_EV_FINAL = 2",
            "LK_EV_ERROR = 3",
        ] {
            assert!(
                h.contains(&squash(proto)),
                "{} lacks {proto}",
                header.display()
            );
        }
    }
}
