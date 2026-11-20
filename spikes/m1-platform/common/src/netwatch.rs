//! The network monitor behind check 7 (PLAN.md M1 1a "network silence during
//! DictationTranscriber, checked with a network monitor"; PRD §7.1 engine A1: "The spike confirms
//! with a network monitor that it makes no network calls").
//!
//! The monitor is macOS's own `nettop` in delta logging mode:
//!
//! ```text
//! script -q /dev/null /usr/bin/nettop -P -L 0 -d -s 1 -n -x -J time,bytes_in,bytes_out
//! ```
//!
//! `nettop` buffers its output when it writes to a pipe (measured on 19 Nov 2026: four 1 s
//! samples arrived together at exit), so it runs under `script`, which gives it a pseudo-terminal
//! and makes each sample arrive when it is taken. Each sample starts with a header line
//! (`time,,bytes_in,bytes_out,`) followed by one row per process (`HH:MM:SS.micro,name.pid,in,out,`).
//! The first sample is cumulative and is ignored; every later sample holds the bytes each process
//! moved since the previous one, including sockets that opened and closed in between.
//!
//! **Grouping.** nettop cuts process names to 15 characters, and two truncations are ambiguous
//! on macOS 26.5.1: `com.apple.Speec` is both the SpeechRecognitionCore broker and the speech
//! synthesis daemon, and `com.apple.siri.` is both the embedded speech recogniser and other Siri
//! services. So the app resolves each pid that moved bytes to its executable name
//! ([`exe_name`]) and [`classify`] prefers that name. The groups are:
//! - [`Group::SelfProc`] and [`Group::Speech`] (the spike, and the speech-recognition processes
//!   found on disk): any byte in a trial window fails check 7;
//! - [`Group::Background`] (speech-adjacent and general-purpose daemons such as Siri, dictation,
//!   asset and experiment daemons, `nsurlsessiond`): they move data for many clients, so any byte
//!   makes the run inconclusive, never a pass; the Wi-Fi-off run of check 6 decides;
//! - [`Group::Other`]: counted, never named.

use std::collections::{BTreeSet, HashMap};

use serde::Serialize;

/// The command line the app runs (through `/usr/bin/script` for a pseudo-terminal).
pub const NETTOP_ARGS: [&str; 13] = [
    "-q",
    "/dev/null",
    "/usr/bin/nettop",
    "-P",
    "-L",
    "0",
    "-d",
    "-s",
    "1",
    "-n",
    "-x",
    "-J",
    "time,bytes_in,bytes_out",
];

/// Bytes one process moved in one sample.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProcBytes {
    /// nettop's name (at most 15 characters).
    pub name: String,
    pub pid: u32,
    pub bytes_in: u64,
    pub bytes_out: u64,
    /// The executable's file name, resolved from the pid by the app (see [`resolve_exes`]).
    pub exe: Option<String>,
}

/// How a process is treated by the silence check.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Group {
    /// The spike app itself (the speech library runs in its process).
    SelfProc,
    /// Speech-recognition processes: any byte fails the check.
    Speech,
    /// Speech-adjacent or general-purpose daemons: any byte makes the run inconclusive.
    Background,
    /// Everything else (reported only as a count and a byte total, never by name).
    Other,
}

/// Speech-recognition executables on macOS 26.5.1 (found on disk on 19 Nov 2026), lower case,
/// matched against the resolved executable name:
/// - `localspeechrecognition`: Speech.framework's on-device recognition XPC service;
/// - `com.apple.siri.embeddedspeech`: CoreEmbeddedSpeechRecognition's recogniser XPC service;
/// - `com.apple.SpeechRecognitionCore.brokerd`: SpeechRecognitionCore's broker XPC service;
/// - `speechrecognitiond`, `speechanalyzer*`: names used by other releases.
const SPEECH_EXES: [&str; 5] = [
    "localspeechrecognition",
    "com.apple.siri.embeddedspeech",
    "com.apple.speechrecognitioncore",
    "speechrecognitiond",
    "speechanalyzer",
];

/// The same, as 15-character nettop names, for rows whose executable could not be resolved.
/// Only unambiguous truncations are listed.
const SPEECH_TRUNCATED: [&str; 3] = ["localspeechreco", "speechrecogniti", "speechanalyzer"];

/// Speech-adjacent and general-purpose daemons, lower case, matched against both names:
/// corespeechd, speechmaintenanced, SpeechDataInstallerd, speech synthesis (`speec`); Siri,
/// assistantd and friends; DictationIM; asset daemons (mobileassetd, languageassetd,
/// assetsubscriptiond, AssetCache*); MobileSoftwareUpdate (`com.apple.mobil`); `triald`
/// (experiment and model delivery); `nsurlsessiond` (background transfers for every app).
const BACKGROUND: [&str; 8] = [
    "speec",
    "siri",
    "assistant",
    "dictation",
    "asset",
    "com.apple.mobil",
    "triald",
    "nsurlsessiond",
];

/// Classifies a process for the silence check. `exe` is the resolved executable name, when known.
pub fn classify(name: &str, exe: Option<&str>, pid: u32, self_pid: u32) -> Group {
    if pid == self_pid {
        return Group::SelfProc;
    }
    let name_l = name.to_ascii_lowercase();
    let exe_l = exe.map(str::to_ascii_lowercase);
    let speech = match &exe_l {
        Some(e) => SPEECH_EXES.iter().any(|p| e.contains(p)),
        None => SPEECH_TRUNCATED.iter().any(|p| name_l.contains(p)),
    };
    if speech {
        return Group::Speech;
    }
    let background = BACKGROUND
        .iter()
        .any(|p| name_l.contains(p) || exe_l.as_deref().is_some_and(|e| e.contains(p)));
    if background {
        Group::Background
    } else {
        Group::Other
    }
}

/// The executable file name of a running process (`proc_pidpath`, no permission needed), or
/// `None` once it has exited. Only the file name is returned, never the folder.
pub fn exe_name(pid: u32) -> Option<String> {
    let pid = libc::c_int::try_from(pid).ok()?;
    let mut buf = vec![0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
    // SAFETY: buf is writable for the length passed; proc_pidpath writes at most that many bytes
    // and returns the length written, or <= 0 on failure.
    let n = unsafe { libc::proc_pidpath(pid, buf.as_mut_ptr().cast(), buf.len() as u32) };
    if n <= 0 {
        return None;
    }
    buf.truncate(usize::try_from(n).ok()?);
    let path = String::from_utf8(buf).ok()?;
    let file = path.rsplit('/').next()?.to_string();
    (!file.is_empty()).then_some(file)
}

/// Fills `exe` for every row that moved bytes, through `resolve` (normally [`exe_name`]), with a
/// per-pid cache so each pid is looked up once.
pub fn resolve_exes(
    sample: &mut Sample,
    cache: &mut HashMap<u32, Option<String>>,
    resolve: impl Fn(u32) -> Option<String>,
) {
    for r in &mut sample.rows {
        if r.exe.is_none() && r.bytes_in + r.bytes_out > 0 {
            r.exe = cache.entry(r.pid).or_insert_with(|| resolve(r.pid)).clone();
        }
    }
}

/// One complete delta sample, with the host-clock interval it covers.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Sample {
    /// Index from 0; sample 0 is the cumulative baseline.
    pub index: u64,
    /// Host time the previous sample's header arrived (start of this sample's interval).
    pub start_host_ns: u64,
    /// Host time this sample's header arrived (end of its interval).
    pub end_host_ns: u64,
    pub rows: Vec<ProcBytes>,
    /// Rows that could not be parsed.
    pub bad_rows: usize,
}

impl Sample {
    /// True for the first, cumulative sample.
    pub fn is_baseline(&self) -> bool {
        self.index == 0
    }
}

/// Incremental parser for nettop's output. Feed it lines with their arrival time; it returns a
/// sample once the next header shows the sample is complete (or on [`Parser::finish`]).
#[derive(Debug, Default)]
pub struct Parser {
    name_col: Option<usize>,
    in_col: Option<usize>,
    out_col: Option<usize>,
    index: u64,
    prev_header_ns: Option<u64>,
    current: Option<Sample>,
}

impl Parser {
    pub fn new() -> Self {
        Self::default()
    }

    /// Cleans pseudo-terminal artefacts: `\r`, and the `^D` echo plus two backspaces that
    /// `script` puts before the first line when its stdin is /dev/null (recorded 19 Nov 2026:
    /// bytes `5e 44 08 08`), or a raw EOT byte.
    fn clean(line: &str) -> &str {
        let l = line.trim_end_matches(['\r', '\n']);
        let l = l.trim_start_matches('\u{4}');
        let l = l.strip_prefix("^D").unwrap_or(l);
        l.trim_start_matches('\u{8}')
    }

    /// Feeds one line received at `host_ns`.
    pub fn feed(&mut self, line: &str, host_ns: u64) -> Option<Sample> {
        let line = Self::clean(line);
        if line.is_empty() {
            return None;
        }
        let fields: Vec<&str> = line.split(',').collect();
        if fields.contains(&"bytes_in") && fields.contains(&"bytes_out") {
            return self.header(&fields, host_ns);
        }
        let cur = self.current.as_mut()?;
        let (Some(nc), Some(ic), Some(oc)) = (self.name_col, self.in_col, self.out_col) else {
            cur.bad_rows += 1;
            return None;
        };
        match parse_row(&fields, nc, ic, oc) {
            Some(row) => cur.rows.push(row),
            None => cur.bad_rows += 1,
        }
        None
    }

    fn header(&mut self, fields: &[&str], host_ns: u64) -> Option<Sample> {
        self.in_col = fields.iter().position(|f| *f == "bytes_in");
        self.out_col = fields.iter().position(|f| *f == "bytes_out");
        // The process column is the first unnamed one ("time,,bytes_in,..." or ",bytes_in,...").
        self.name_col = fields.iter().position(|f| f.is_empty());
        let done = self.current.take();
        let start = self.prev_header_ns.unwrap_or(host_ns);
        self.current = Some(Sample {
            index: self.index,
            start_host_ns: start,
            end_host_ns: host_ns,
            rows: Vec::new(),
            bad_rows: 0,
        });
        self.index += 1;
        self.prev_header_ns = Some(host_ns);
        done
    }

    /// Returns the last, still open sample (call when the stream ends).
    pub fn finish(&mut self) -> Option<Sample> {
        self.current.take()
    }
}

/// Parses `name.pid` plus the byte columns of one row.
fn parse_row(fields: &[&str], name_col: usize, in_col: usize, out_col: usize) -> Option<ProcBytes> {
    let who = fields.get(name_col)?;
    let (name, pid) = who.rsplit_once('.')?;
    let pid: u32 = pid.parse().ok()?;
    let bytes_in: u64 = fields.get(in_col)?.trim().parse().ok()?;
    let bytes_out: u64 = fields.get(out_col)?.trim().parse().ok()?;
    if name.is_empty() {
        return None;
    }
    Some(ProcBytes {
        name: name.to_string(),
        pid,
        bytes_in,
        bytes_out,
        exe: None,
    })
}

/// Bytes per group within a time window, and whether the samples cover all of it.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct WindowBytes {
    pub samples: usize,
    /// Start of the first and end of the last delta sample overlapping the window.
    pub covered_from_ns: Option<u64>,
    pub covered_to_ns: Option<u64>,
    /// The overlapping delta samples are consecutive and span the whole window, so no byte in
    /// the window can have gone unexamined.
    pub covered: bool,
    pub self_bytes: u64,
    pub speech_bytes: u64,
    pub background_bytes: u64,
    pub other_bytes: u64,
    pub other_processes: usize,
    /// Named rows for the self, speech and background groups only (system daemons).
    pub flagged: Vec<FlaggedRow>,
}

/// A process in a watched group that moved bytes.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FlaggedRow {
    pub group: Group,
    pub name: String,
    pub exe: Option<String>,
    pub pid: u32,
    pub bytes_in: u64,
    pub bytes_out: u64,
    pub sample_index: u64,
}

impl WindowBytes {
    /// Bytes from the spike and the speech-recognition processes: any of them fails check 7.
    pub fn failing_bytes(&self) -> u64 {
        self.self_bytes + self.speech_bytes
    }

    /// The background daemons that moved bytes (executable name when resolved), deduplicated.
    pub fn background_names(&self) -> Vec<String> {
        let names: BTreeSet<String> = self
            .flagged
            .iter()
            .filter(|f| f.group == Group::Background)
            .map(|f| f.exe.clone().unwrap_or_else(|| f.name.clone()))
            .collect();
        names.into_iter().collect()
    }
}

/// Sums the delta samples that overlap `[start_ns, end_ns]`, by group, and checks that they
/// cover the window. Baseline samples are skipped.
pub fn window_bytes(samples: &[Sample], start_ns: u64, end_ns: u64, self_pid: u32) -> WindowBytes {
    let mut w = WindowBytes::default();
    let mut others = BTreeSet::new();
    let mut overlapping: Vec<&Sample> = samples
        .iter()
        .filter(|s| !s.is_baseline() && s.end_host_ns >= start_ns && s.start_host_ns <= end_ns)
        .collect();
    overlapping.sort_by_key(|s| s.index);
    for s in &overlapping {
        w.samples += 1;
        for r in &s.rows {
            let bytes = r.bytes_in + r.bytes_out;
            if bytes == 0 {
                continue;
            }
            let g = classify(&r.name, r.exe.as_deref(), r.pid, self_pid);
            match g {
                Group::SelfProc => w.self_bytes += bytes,
                Group::Speech => w.speech_bytes += bytes,
                Group::Background => w.background_bytes += bytes,
                Group::Other => {
                    w.other_bytes += bytes;
                    others.insert(r.pid);
                }
            }
            if g != Group::Other {
                w.flagged.push(FlaggedRow {
                    group: g,
                    name: r.name.clone(),
                    exe: r.exe.clone(),
                    pid: r.pid,
                    bytes_in: r.bytes_in,
                    bytes_out: r.bytes_out,
                    sample_index: s.index,
                });
            }
        }
    }
    w.other_processes = others.len();
    w.covered_from_ns = overlapping.first().map(|s| s.start_host_ns);
    w.covered_to_ns = overlapping.last().map(|s| s.end_host_ns);
    let consecutive = overlapping.windows(2).all(|p| p[1].index == p[0].index + 1);
    w.covered = consecutive
        && w.covered_from_ns.is_some_and(|f| f <= start_ns)
        && w.covered_to_ns.is_some_and(|t| t >= end_ns);
    w
}

/// Whether the machine had a route to the network: the interfaces that are up and running with
/// a routable address (not loopback, not link-local). Recorded with check 7 so the Wi-Fi-off
/// cross-check run shows that it really was offline. Interface names (`en0`, `utun3`) only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct NetState {
    pub online: bool,
    pub routable_interfaces: Vec<String>,
    pub error: Option<String>,
}

/// An IPv4 address that can reach beyond this machine and its link (not 0/8, 127/8, 169.254/16).
pub fn is_routable_v4(a: [u8; 4]) -> bool {
    !(a[0] == 0 || a[0] == 127 || (a[0] == 169 && a[1] == 254))
}

/// An IPv6 address that can reach beyond this machine and its link (not ::, ::1, fe80::/10).
pub fn is_routable_v6(a: [u8; 16]) -> bool {
    let unspecified_or_loopback = a[..15].iter().all(|&b| b == 0) && a[15] <= 1;
    let link_local = a[0] == 0xfe && (a[1] & 0xc0) == 0x80;
    !(unspecified_or_loopback || link_local)
}

/// Reads the interface list (`getifaddrs`, no permission needed).
pub fn net_state() -> NetState {
    let mut names = BTreeSet::new();
    let mut ifap: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs stores a list head in ifap, which is freed below with freeifaddrs.
    if unsafe { libc::getifaddrs(&mut ifap) } != 0 {
        return NetState {
            online: false,
            routable_interfaces: Vec::new(),
            error: Some(std::io::Error::last_os_error().to_string()),
        };
    }
    let mut p = ifap;
    while !p.is_null() {
        // SAFETY: p is a node of the list getifaddrs returned; it stays valid until freeifaddrs.
        let ifa = unsafe { &*p };
        p = ifa.ifa_next;
        let flags = ifa.ifa_flags;
        let up = flags & (libc::IFF_UP as libc::c_uint) != 0
            && flags & (libc::IFF_RUNNING as libc::c_uint) != 0;
        let loopback = flags & (libc::IFF_LOOPBACK as libc::c_uint) != 0;
        if !up || loopback || ifa.ifa_addr.is_null() || ifa.ifa_name.is_null() {
            continue;
        }
        // SAFETY: ifa_addr is non-null and points to a sockaddr owned by the list.
        let family = libc::c_int::from(unsafe { (*ifa.ifa_addr).sa_family });
        let routable = match family {
            libc::AF_INET => {
                // SAFETY: an AF_INET address is a sockaddr_in.
                let sin = unsafe { &*(ifa.ifa_addr as *const libc::sockaddr_in) };
                is_routable_v4(sin.sin_addr.s_addr.to_ne_bytes())
            }
            libc::AF_INET6 => {
                // SAFETY: an AF_INET6 address is a sockaddr_in6.
                let sin6 = unsafe { &*(ifa.ifa_addr as *const libc::sockaddr_in6) };
                is_routable_v6(sin6.sin6_addr.s6_addr)
            }
            _ => false,
        };
        if routable {
            // SAFETY: ifa_name is a non-null NUL-terminated string owned by the list.
            let name = unsafe { std::ffi::CStr::from_ptr(ifa.ifa_name) };
            names.insert(name.to_string_lossy().into_owned());
        }
    }
    // SAFETY: ifap came from a successful getifaddrs and is freed exactly once.
    unsafe { libc::freeifaddrs(ifap) };
    NetState {
        online: !names.is_empty(),
        routable_interfaces: names.into_iter().collect(),
        error: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: u64 = 1_000_000_000;

    /// A fixture in the format nettop writes on macOS 26.5.1 under `script` with stdin
    /// /dev/null: the first header starts with the `^D` echo and two backspaces and lines end in
    /// CRLF (recorded 19 Nov 2026 from the header lines only). The rows' numbers and most names
    /// are made up; `com.apple.Speec` is the ambiguous truncation.
    const FIXTURE: &str = "^D\u{8}\u{8}time,,bytes_in,bytes_out,\r\n\
        13:37:13.960700,syslogd.370,0,15979,\r\n\
        13:37:13.960701,apsd.376,1350325,4158043,\r\n\
        13:37:13.960702,localspeechreco.9001,120,0,\r\n\
        time,,bytes_in,bytes_out,\r\n\
        13:37:14.960700,syslogd.370,0,0,\r\n\
        13:37:14.960701,apsd.376,90,310,\r\n\
        13:37:14.960702,localspeechreco.9001,0,0,\r\n\
        13:37:14.960703,m1-spike.4242,0,0,\r\n\
        time,,bytes_in,bytes_out,\r\n\
        13:37:15.960700,com.apple.Speec.777,0,512,\r\n\
        13:37:15.960701,mobileassetd.55,1000,20,\r\n\
        13:37:15.960702,Google Chrome H.60734,4,4,\r\n\
        13:37:15.960703,m1-spike.4242,8,0,\r\n\
        garbage\r\n\
        time,,bytes_in,bytes_out,\r\n\
        13:37:16.960700,localspeechreco.9001,64,64,\r\n";

    fn parse_fixture() -> Vec<Sample> {
        let mut p = Parser::new();
        let mut out = Vec::new();
        let mut headers = 0u64;
        for (k, line) in FIXTURE.split('\n').enumerate() {
            // Headers arrive 1 s apart; rows a few ms after their header.
            let t = if line.contains("bytes_in") {
                headers += 1;
                100 * S + (headers - 1) * S
            } else {
                100 * S + (headers.saturating_sub(1)) * S + (k as u64) * 1_000_000
            };
            if let Some(s) = p.feed(line, t) {
                out.push(s);
            }
        }
        out.extend(p.finish());
        out
    }

    #[test]
    fn parses_samples_rows_and_intervals() {
        let s = parse_fixture();
        assert_eq!(s.len(), 4);
        assert!(s[0].is_baseline());
        assert_eq!(s[0].rows.len(), 3);
        assert_eq!(
            s[1].rows[1],
            ProcBytes {
                name: "apsd".into(),
                pid: 376,
                bytes_in: 90,
                bytes_out: 310,
                exe: None,
            }
        );
        assert_eq!(s[2].rows[2].name, "Google Chrome H");
        assert_eq!(s[2].bad_rows, 1);
        assert_eq!(s[1].start_host_ns, s[0].end_host_ns);
        assert!(s[2].end_host_ns > s[1].end_host_ns);
        assert_eq!(s.iter().map(|x| x.bad_rows).sum::<usize>(), 1);
    }

    #[test]
    fn the_first_header_is_found_after_the_eot_echo_and_backspaces() {
        let mut p = Parser::new();
        assert!(
            p.feed("^D\u{8}\u{8}time,,bytes_in,bytes_out,\r\n", 1)
                .is_none()
        );
        assert!(p.feed("10:00:00.1,x.1,0,0,\r\n", 2).is_none());
        let s = p
            .feed("time,,bytes_in,bytes_out,\r\n", 3)
            .expect("sample 0");
        assert_eq!((s.index, s.rows.len(), s.bad_rows), (0, 1, 0));
    }

    #[test]
    fn classifies_recognisers_background_daemons_and_ambiguous_truncations() {
        let g = |name: &str, exe: Option<&str>| classify(name, exe, 1, 2);
        // Speech-recognition processes (full executable names found on disk, and the
        // unambiguous truncations).
        assert_eq!(
            g("localspeechreco", Some("localspeechrecognition")),
            Group::Speech
        );
        assert_eq!(g("localspeechreco", None), Group::Speech);
        assert_eq!(
            g(
                "com.apple.Speec",
                Some("com.apple.SpeechRecognitionCore.brokerd")
            ),
            Group::Speech
        );
        assert_eq!(
            g("com.apple.siri.", Some("com.apple.siri.embeddedspeech")),
            Group::Speech
        );
        // The same truncations resolve to unrelated daemons too, and unresolved they are only
        // background.
        assert_eq!(
            g("com.apple.Speec", Some("com.apple.speech.speechsynthesisd")),
            Group::Background
        );
        assert_eq!(g("com.apple.Speec", None), Group::Background);
        assert_eq!(g("com.apple.siri.", None), Group::Background);
        // Speech-adjacent and general-purpose daemons.
        for (name, exe) in [
            ("corespeechd", Some("corespeechd")),
            ("speechmaintenan", None),
            ("assistantd", Some("assistantd")),
            ("siriknowledged", None),
            ("DictationIM", Some("DictationIM")),
            ("mobileassetd", Some("mobileassetd")),
            ("languageassetd", None),
            ("triald", Some("triald")),
            ("nsurlsessiond", None),
        ] {
            assert_eq!(g(name, exe), Group::Background, "{name}");
        }
        assert_eq!(g("apsd", Some("apsd")), Group::Other);
        assert_eq!(g("trialsd-typo", None), Group::Other);
        assert_eq!(classify("anything", None, 7, 7), Group::SelfProc);
    }

    #[test]
    fn window_sums_skip_the_baseline_and_hide_other_names() {
        let mut s = parse_fixture();
        let mut cache = HashMap::new();
        for x in &mut s {
            resolve_exes(x, &mut cache, |pid| {
                (pid == 777).then(|| "com.apple.speech.speechsynthesisd".to_string())
            });
        }
        // Each pid that moved bytes was looked up once (7 pids, some in several samples).
        assert_eq!(cache.len(), 7);
        // Window inside the second delta sample only (samples touching a window's edge count,
        // which errs towards attributing bytes to the window).
        let w = window_bytes(&s, s[2].start_host_ns + 1, s[2].end_host_ns - 1, 4242);
        assert_eq!(w.samples, 1);
        assert!(w.covered);
        assert_eq!(
            (w.self_bytes, w.speech_bytes, w.background_bytes),
            (8, 0, 512 + 1020)
        );
        assert_eq!((w.other_bytes, w.other_processes), (8, 1));
        assert_eq!(w.failing_bytes(), 8);
        assert_eq!(
            w.background_names(),
            vec!["com.apple.speech.speechsynthesisd", "mobileassetd"]
        );
        assert!(w.flagged.iter().all(|f| f.group != Group::Other));
        // The first delta sample: apsd only, nothing watched; the baseline's speech bytes are
        // cumulative and must not count.
        let w1 = window_bytes(&s, s[1].start_host_ns + 1, s[1].end_host_ns - 1, 4242);
        assert_eq!(w1.failing_bytes() + w1.background_bytes, 0);
        assert_eq!(w1.other_bytes, 400);
        // The last sample: the recogniser moved bytes (unresolved, by its truncated name).
        let w3 = window_bytes(&s, s[3].start_host_ns + 1, s[3].end_host_ns, 4242);
        assert_eq!((w3.samples, w3.speech_bytes), (1, 128));
        assert_eq!(w3.failing_bytes(), 128);
    }

    /// The reviewer's case: samples ending at 103 s and a window running to 105 s. The window's
    /// tail is unexamined, so it is not covered.
    #[test]
    fn a_window_is_covered_only_when_samples_span_all_of_it() {
        let sample = |index: u64, start_s: u64| Sample {
            index,
            start_host_ns: start_s * S,
            end_host_ns: (start_s + 1) * S,
            rows: Vec::new(),
            bad_rows: 0,
        };
        let s: Vec<Sample> = (0..4u64).map(|k| sample(k, 99 + k)).collect();
        let tail = window_bytes(&s, 100 * S + S / 2, 105 * S, 1);
        assert_eq!(tail.samples, 3);
        assert!(!tail.covered, "{tail:?}");
        assert_eq!(tail.covered_to_ns, Some(103 * S));
        let inside = window_bytes(&s, 100 * S + S / 2, 102 * S + S / 2, 1);
        assert!(inside.covered);
        // A window starting before the first delta sample (inside the baseline) is not covered.
        assert!(!window_bytes(&s, 99 * S + S / 2, 101 * S, 1).covered);
        // A missing sample in the middle leaves a gap.
        let gap: Vec<Sample> = s.iter().filter(|x| x.index != 2).cloned().collect();
        assert!(!window_bytes(&gap, 100 * S + S / 2, 102 * S + S / 2, 1).covered);
        assert!(!window_bytes(&[], 1, 2, 1).covered);
    }

    #[test]
    fn resolves_executable_names_by_pid() {
        let me = exe_name(std::process::id()).expect("own executable");
        assert!(!me.contains('/'), "{me}");
        assert!(me.starts_with("m1_spike_common"), "{me}");
        // pid 1 is launchd.
        assert_eq!(exe_name(1).as_deref(), Some("launchd"));
        assert_eq!(exe_name(u32::MAX), None);
    }

    #[test]
    fn routable_addresses_and_the_interface_list() {
        assert!(is_routable_v4([192, 168, 1, 20]));
        assert!(is_routable_v4([10, 0, 0, 1]));
        assert!(!is_routable_v4([127, 0, 0, 1]));
        assert!(!is_routable_v4([169, 254, 3, 4]));
        assert!(!is_routable_v4([0, 0, 0, 0]));
        let mut v6 = [0u8; 16];
        assert!(!is_routable_v6(v6));
        v6[15] = 1;
        assert!(!is_routable_v6(v6));
        let mut ll = [0u8; 16];
        ll[0] = 0xfe;
        ll[1] = 0x80;
        ll[15] = 7;
        assert!(!is_routable_v6(ll));
        let mut global = [0u8; 16];
        global[0] = 0x26;
        global[1] = 0x01;
        global[15] = 9;
        assert!(is_routable_v6(global));
        // A real read of this machine's interfaces (whatever its state).
        let n = net_state();
        assert!(n.error.is_none(), "{n:?}");
        assert_eq!(n.online, !n.routable_interfaces.is_empty());
        assert!(n.routable_interfaces.iter().all(|i| !i.starts_with("lo")));
    }
}
