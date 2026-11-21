import Darwin
import Foundation
import SnowGlobeSpeechC
import Synchronization

/// How `Replay.run` feeds a file to the speech library.
public struct ReplayOptions: Sendable, Equatable {
    public var locale = "en-US"
    /// Samples per push, in milliseconds of audio (a microphone callback delivers ~10 ms).
    public var chunkMs = 10.0
    /// Push at the audio's own pace, like a live microphone (true), or fast (false): at up to
    /// `fastSpeedup` times that pace, waiting for READY once `fastAheadOfReadySeconds` of audio
    /// are buffered. Nothing drains the library's ring (at least 20 s) until the recognizer is
    /// ready, so unbounded pushing would overflow it on long files and end in ERROR -3.
    public var realtime = true
    public var fastSpeedup = 20.0
    public var fastAheadOfReadySeconds = 15.0
    /// Put transcript text in the event lines. Off: lengths and word counts only (PRD §10.5).
    public var includeText = true
    /// How long to wait for FINAL or ERROR after `sg_speech_finish`.
    public var terminalTimeoutSeconds = 20.0
    /// Passed to `sg_speech_set_contextual_strings` when non-empty.
    public var contextualStrings: [String] = []
    /// Passed to `sg_speech_set_silence_pad_ms` when set.
    public var silencePadMs: Int32?
    /// Passed to `sg_speech_set_model_retention` when set (`SG_SPEECH_RETAIN_*`).
    public var retention: Int32?
    /// The file's name for the meta line (a name only, never a path).
    public var sourceName = ""

    public init() {}
}

/// One callback as the replay received it.
public struct ReceivedEvent: Sendable, Equatable {
    public let kind: UInt32
    public let text: String
    public let audioStart: Double
    public let audioEnd: Double
    public let errorCode: Int32
    /// Host clock (CLOCK_UPTIME_RAW, ns), the clock the M1 spike's `t_host_ns` uses.
    public let hostNs: UInt64
    /// Frames pushed when the callback ran.
    public let framesPushed: Int

    public var kindName: String { Replay.kindName(kind) }
}

/// What one replay produced. `lines` is the JSONL output; `exitCode` 0 means a FINAL arrived.
public struct ReplayOutcome: Sendable {
    public let lines: [String]
    public let events: [ReceivedEvent]
    public let exitCode: Int32
}

/// Feeds a WAV file to the speech library through the C ABI (exactly as the shell does) and
/// writes one JSON line per event with its wall and audio times (PRD §10.5 timestamps, §10.6
/// replay). Never run by an agent on speech: voice fixtures come only from the owner.
///
/// Lines: one `meta`, one `event` per callback, one `summary`. Events are collected while the
/// audio plays and printed afterwards, so writing output never delays a callback.
public enum Replay {
    public static func kindName(_ k: UInt32) -> String {
        switch k {
        case SG_EV_READY.rawValue: "ready"
        case SG_EV_PARTIAL.rawValue: "partial"
        case SG_EV_FINAL.rawValue: "final"
        case SG_EV_ERROR.rawValue: "error"
        case SG_EV_STABLE.rawValue: "stable"
        default: "unknown-\(k)"
        }
    }

    public static func run(audio: WAVAudio, options: ReplayOptions) -> ReplayOutcome {
        var lines: [String] = []
        let settingsRC = applySettings(options)
        let meta: [String: Any] = [
            "type": "meta",
            "tool": "snow-globe-transcribe",
            "library": String(cString: sg_speech_version()),
            "locale": options.locale,
            "file": options.sourceName,
            "sample_rate_hz": audio.sampleRate,
            "channels": audio.channels,
            "format": audio.format,
            "frames": audio.frames,
            "audio_s": audio.seconds,
            "truncated": audio.truncated,
            "chunk_ms": options.chunkMs,
            "pacing": options.realtime ? "realtime" : "fast",
            "fast_speedup": options.realtime ? 1 : options.fastSpeedup,
            // This path has no microphone, cpal or device stage, so its timings are diagnostics:
            // never D-004's release-to-final measure or the PRD §10.6 replay (CLAUDE.md: no stage
            // is mocked in a measured run).
            "timings": "diagnostic: WAV through the C ABI only (no microphone, cpal or device stage); not the PRD 10.6 measure",
            "silence_pad_ms": options.silencePadMs.map { Int($0) } ?? 0,
            "model_retention": options.retention.map { Int($0) } ?? Int(SG_SPEECH_RETAIN_LINGERING),
            "contextual_strings": options.contextualStrings.count,
            "include_text": options.includeText,
            "settings_rc": settingsRC,
        ]
        lines.append(JSONLine.encode(meta))
        if settingsRC != SG_SPEECH_OK {
            lines.append(JSONLine.encode(["type": "summary", "terminal": "none", "error": "a setting was rejected", "settings_rc": settingsRC]))
            return ReplayOutcome(lines: lines, events: [], exitCode: 2)
        }

        let collector = Collector()
        let ctx = Unmanaged.passRetained(collector).toOpaque()
        defer { Unmanaged<Collector>.fromOpaque(ctx).release() }

        let startCalled = hostNow()
        let session = options.locale.withCString { loc in
            sg_speech_start(loc, audio.sampleRate, { ctx, kind, text, a0, a1, code in
                guard let ctx else { return }
                Unmanaged<Collector>.fromOpaque(ctx).takeUnretainedValue()
                    .record(kind: kind.rawValue, text: text, a0: a0, a1: a1, code: code)
            }, ctx)
        }
        let startReturned = hostNow()
        guard let session else {
            lines.append(JSONLine.encode([
                "type": "summary", "terminal": "none", "error": "sg_speech_start returned NULL",
                "start_ms": ms(startReturned - startCalled),
            ]))
            return ReplayOutcome(lines: lines, events: [], exitCode: 1)
        }

        let chunk = max(1, Int((options.chunkMs / 1_000 * audio.sampleRate).rounded()))
        var pushNs: [UInt64] = []
        pushNs.reserveCapacity(audio.frames / chunk + 1)
        var pushErrors: [Int32: Int] = [:]
        var waitedForReadyNs: UInt64?
        let firstPush = hostNow()
        audio.mono.withUnsafeBufferPointer { all in
            guard let base = all.baseAddress else { return }
            var offset = 0
            // Pacing: chunk k is due when the audio up to its end has "played" at `speedup`
            // times its pace, counted from (paceOrigin, paceFrames).
            let speedup = options.realtime ? 1.0 : max(1.0, options.fastSpeedup)
            var paceOrigin = firstPush
            var paceFrames = 0
            var readyChecked = options.realtime
            while offset < all.count {
                let n = min(chunk, all.count - offset)
                if !readyChecked, Double(offset + n) / audio.sampleRate > options.fastAheadOfReadySeconds {
                    let t0 = hostNow()
                    _ = collector.waitReadyOrTerminal(seconds: options.terminalTimeoutSeconds)
                    let t1 = hostNow()
                    waitedForReadyNs = t1 - t0
                    readyChecked = true
                    paceOrigin = t1
                    paceFrames = offset
                }
                // A microphone hands over a buffer once its last sample has been captured, so a
                // chunk is due when the audio up to its end has "played".
                let due = paceOrigin + UInt64(Double(offset + n - paceFrames) / audio.sampleRate / speedup * 1e9)
                sleepUntil(due)
                let t0 = hostNow()
                let rc = sg_speech_push_f32(session, base + offset, n)
                let t1 = hostNow()
                pushNs.append(t1 - t0)
                if rc != SG_SPEECH_OK { pushErrors[rc, default: 0] += 1 }
                offset += n
                collector.framesPushed.store(offset, ordering: .releasing)
            }
        }
        let lastPush = hostNow()
        let finishCalled = hostNow()
        let finishRC = sg_speech_finish(session)
        let finishReturned = hostNow()
        let terminalArrived = finishRC == SG_SPEECH_OK
            ? collector.waitTerminal(seconds: options.terminalTimeoutSeconds)
            : collector.hasTerminal()
        sg_speech_cancel(session)
        sg_speech_free(session)

        let events = collector.events()
        for e in events {
            lines.append(JSONLine.encode(eventLine(e, origin: startCalled, rate: audio.sampleRate, includeText: options.includeText)))
        }
        let terminal = events.first { $0.kind == SG_EV_FINAL.rawValue || $0.kind == SG_EV_ERROR.rawValue }
        let firstPartial = events.first { $0.kind == SG_EV_PARTIAL.rawValue && !$0.text.isEmpty }
        let ready = events.first { $0.kind == SG_EV_READY.rawValue }
        let sorted = pushNs.sorted()
        var summary: [String: Any] = [
            "type": "summary",
            "events": events.count,
            "partials": events.filter { $0.kind == SG_EV_PARTIAL.rawValue }.count,
            "stables": events.filter { $0.kind == SG_EV_STABLE.rawValue }.count,
            "finals": events.filter { $0.kind == SG_EV_FINAL.rawValue }.count,
            "errors": events.filter { $0.kind == SG_EV_ERROR.rawValue }.count,
            "terminal": terminal.map { kindName($0.kind) } ?? "none",
            "terminal_within_timeout": terminalArrived,
            "start_ms": ms(startReturned - startCalled),
            "last_push_wall_s": seconds(lastPush - startCalled),
            "finish_called_wall_s": seconds(finishCalled - startCalled),
            "finish_returned_ms": ms(finishReturned - finishCalled),
            "finish_rc": finishRC,
            "push": [
                "count": pushNs.count,
                "frames_per_push": chunk,
                "max_us": Double(sorted.last ?? 0) / 1e3,
                "p50_us": Double(percentile(sorted, 0.50)) / 1e3,
                "p99_us": Double(percentile(sorted, 0.99)) / 1e3,
                "over_1ms": pushNs.filter { $0 > 1_000_000 }.count,
                "errors": Dictionary(uniqueKeysWithValues: pushErrors.map { (String($0.key), $0.value) }),
            ] as [String: Any],
        ]
        if let ready { summary["ready_wall_s"] = seconds(ready.hostNs - startCalled) }
        if let waitedForReadyNs { summary["fast_waited_for_ready_ms"] = ms(waitedForReadyNs) }
        if let firstPartial { summary["first_partial_wall_s"] = seconds(firstPartial.hostNs - startCalled) }
        if let terminal {
            summary["terminal_wall_s"] = seconds(terminal.hostNs - startCalled)
            // The library's own finish-to-terminal time, a diagnostic: the file's end stands in
            // for key-up, and there is no microphone, cpal or device stage (meta "timings").
            summary["finish_to_terminal_ms"] = terminal.hostNs >= finishCalled ? ms(terminal.hostNs - finishCalled) : -ms(finishCalled - terminal.hostNs)
            summary["error_code"] = terminal.errorCode
        }
        lines.append(JSONLine.encode(summary))
        let ok = terminal?.kind == SG_EV_FINAL.rawValue
        return ReplayOutcome(lines: lines, events: events, exitCode: ok ? 0 : 1)
    }

    static func applySettings(_ o: ReplayOptions) -> Int32 {
        var rc = SG_SPEECH_OK
        if !o.contextualStrings.isEmpty {
            rc = withCStringArray(o.contextualStrings) { sg_speech_set_contextual_strings($0, o.contextualStrings.count) }
            if rc != SG_SPEECH_OK { return rc }
        }
        if let pad = o.silencePadMs {
            rc = sg_speech_set_silence_pad_ms(pad)
            if rc != SG_SPEECH_OK { return rc }
        }
        if let r = o.retention {
            rc = sg_speech_set_model_retention(r)
        }
        return rc
    }

    static func eventLine(_ e: ReceivedEvent, origin: UInt64, rate: Double, includeText: Bool) -> [String: Any] {
        var d: [String: Any] = [
            "type": "event",
            "kind": e.kindName,
            "host_ns": e.hostNs,
            "wall_s": seconds(e.hostNs >= origin ? e.hostNs - origin : 0),
            "audio_pushed_s": Double(e.framesPushed) / rate,
            "audio_start_s": e.audioStart,
            "audio_end_s": e.audioEnd,
            "error_code": e.errorCode,
            "text_len": e.text.count,
            "words": e.text.split(whereSeparator: \.isWhitespace).count,
        ]
        // An ERROR's text is a diagnostic, never a transcript, so it is always kept.
        if includeText || e.kind == SG_EV_ERROR.rawValue { d["text"] = e.text }
        return d
    }

    static func percentile(_ sorted: [UInt64], _ q: Double) -> UInt64 {
        guard !sorted.isEmpty else { return 0 }
        let i = min(sorted.count - 1, max(0, Int((q * Double(sorted.count - 1)).rounded())))
        return sorted[i]
    }
}

/// Collects callbacks on the library's queue, with the host time of each.
final class Collector: Sendable {
    let framesPushed = Atomic<Int>(0)
    private let received = Mutex<[ReceivedEvent]>([])
    private let terminal = DispatchSemaphore(value: 0)
    private let readyOrEnd = DispatchSemaphore(value: 0)

    func record(kind: UInt32, text: UnsafePointer<CChar>?, a0: Double, a1: Double, code: Int32) {
        let now = hostNow()
        let s = text.map { String(cString: $0) } ?? ""
        let e = ReceivedEvent(
            kind: kind, text: s, audioStart: a0, audioEnd: a1, errorCode: code, hostNs: now,
            framesPushed: framesPushed.load(ordering: .acquiring))
        received.withLock { $0.append(e) }
        if kind == SG_EV_FINAL.rawValue || kind == SG_EV_ERROR.rawValue { terminal.signal() }
        if kind == SG_EV_READY.rawValue || kind == SG_EV_FINAL.rawValue || kind == SG_EV_ERROR.rawValue {
            readyOrEnd.signal()
        }
    }

    /// Waits until READY or a terminal event has arrived (true), or `seconds` pass (false).
    func waitReadyOrTerminal(seconds: Double) -> Bool {
        if hasReadyOrTerminal() { return true }
        _ = readyOrEnd.wait(timeout: .now() + seconds)
        return hasReadyOrTerminal()
    }

    func hasReadyOrTerminal() -> Bool {
        received.withLock { list in
            list.contains { [SG_EV_READY.rawValue, SG_EV_FINAL.rawValue, SG_EV_ERROR.rawValue].contains($0.kind) }
        }
    }

    func waitTerminal(seconds: Double) -> Bool {
        terminal.wait(timeout: .now() + seconds) == .success
    }

    func hasTerminal() -> Bool {
        received.withLock { $0.contains { $0.kind == SG_EV_FINAL.rawValue || $0.kind == SG_EV_ERROR.rawValue } }
    }

    func events() -> [ReceivedEvent] { received.withLock { $0 } }
}

/// The host clock, CLOCK_UPTIME_RAW in nanoseconds (mach_absolute_time on Apple silicon).
func hostNow() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }

func sleepUntil(_ deadline: UInt64) {
    let now = hostNow()
    guard deadline > now else { return }
    var request = timespec(tv_sec: Int((deadline - now) / 1_000_000_000), tv_nsec: Int((deadline - now) % 1_000_000_000))
    var remaining = timespec()
    while nanosleep(&request, &remaining) != 0 && errno == EINTR {
        request = remaining
    }
}

func ms(_ ns: UInt64) -> Double { Double(ns) / 1e6 }
func seconds(_ ns: UInt64) -> Double { Double(ns) / 1e9 }

/// Calls `body` with a C array of C strings that stay valid for the call.
func withCStringArray<R>(_ strings: [String], _ body: (UnsafePointer<UnsafePointer<CChar>?>) -> R) -> R {
    let dup = strings.map { strdup($0) }
    defer { dup.forEach { free($0) } }
    let ptrs: [UnsafePointer<CChar>?] = dup.map { $0.map { UnsafePointer($0) } }
    return ptrs.withUnsafeBufferPointer { body($0.baseAddress!) }
}

/// One JSON object per line, keys sorted, so runs diff cleanly.
enum JSONLine {
    static func encode(_ object: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(object),
            let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]),
            let s = String(data: data, encoding: .utf8)
        else { return "{\"type\":\"encode-error\"}" }
        return s
    }
}

/// Names for `sg_speech_status` and error codes (include/snow_globe_speech.h).
public enum StatusText {
    public static func meaning(_ code: Int32) -> String {
        switch code {
        case SG_SPEECH_OK: "ready"
        case SG_SPEECH_ASSET_NOT_INSTALLED: "model asset not installed"
        case SG_SPEECH_NOT_AUTHORIZED: "not authorized"
        case SG_SPEECH_LOCALE_UNSUPPORTED: "locale unsupported"
        case SG_SPEECH_ERR_ARGUMENT: "invalid argument"
        case SG_SPEECH_ERR_STATE: "wrong state"
        case SG_SPEECH_ERR_OVERFLOW: "ring buffer overflow"
        case SG_SPEECH_ERR_AUDIO_FORMAT: "audio format"
        case SG_SPEECH_ERR_ENGINE: "recognizer failure"
        case SG_SPEECH_ERR_TIMEOUT: "timeout"
        case SG_SPEECH_ERR_RESOURCES: "too many simultaneous analyses"
        case SG_SPEECH_ERR_ASSET_RESERVATION: "model not reserved for this app"
        case SG_SPEECH_ERR_INTERNAL: "internal error"
        default: "undocumented code"
        }
    }

    /// One JSON line for `snow-globe-transcribe status`.
    public static func statusLine(locale: String, code: Int32) -> String {
        JSONLine.encode(["type": "status", "locale": locale, "code": code, "meaning": meaning(code)])
    }
}
