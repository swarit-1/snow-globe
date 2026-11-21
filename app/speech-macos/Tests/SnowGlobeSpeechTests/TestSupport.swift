import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import Synchronization
import XCTest

/// One callback as a test saw it.
struct SeenEvent: Equatable {
    let kind: UInt32
    let text: String
    let start: Double
    let end: Double
    let code: Int32
    let onMainThread: Bool
    let afterFinishCall: Bool
    let afterCancelReturned: Bool
}

/// The `ctx` behind the C callback: records every event and flags any that breaks the rules.
final class Recorder: @unchecked Sendable {
    private let lock = NSLock()
    private var seen: [SeenEvent] = []
    private let terminalSignal = DispatchSemaphore(value: 0)
    private let anySignal = DispatchSemaphore(value: 0)
    /// Set by the test just before it calls sg_speech_finish.
    let finishCalled = Atomic<Bool>(false)
    /// Set by the test right after sg_speech_cancel or sg_speech_free returns.
    let cancelReturned = Atomic<Bool>(false)
    /// Callbacks that started after cancelReturned was set, or that overlapped another callback.
    let violations = Atomic<Int>(0)
    private let inCallback = Atomic<Int>(0)
    /// Runs inside the callback, after the event is recorded.
    var hook: ((SeenEvent) -> Void)?

    var context: UnsafeMutableRawPointer { Unmanaged.passUnretained(self).toOpaque() }

    func receive(_ kind: UInt32, _ text: UnsafePointer<CChar>?, _ a0: Double, _ a1: Double, _ code: Int32) {
        if inCallback.wrappingAdd(1, ordering: .acquiringAndReleasing).newValue > 1 {
            _ = violations.wrappingAdd(1, ordering: .relaxed)
        }
        defer { _ = inCallback.wrappingSubtract(1, ordering: .acquiringAndReleasing) }
        let late = cancelReturned.load(ordering: .acquiring)
        if late { _ = violations.wrappingAdd(1, ordering: .relaxed) }
        let e = SeenEvent(
            kind: kind, text: text.map { String(cString: $0) } ?? "<NULL>", start: a0, end: a1, code: code,
            onMainThread: pthread_main_np() != 0, afterFinishCall: finishCalled.load(ordering: .acquiring),
            afterCancelReturned: late)
        lock.lock()
        seen.append(e)
        let h = hook
        lock.unlock()
        anySignal.signal()
        if kind == SG_EV_FINAL.rawValue || kind == SG_EV_ERROR.rawValue { terminalSignal.signal() }
        h?(e)
    }

    var events: [SeenEvent] {
        lock.lock()
        defer { lock.unlock() }
        return seen
    }

    func count(_ kind: sg_event_kind) -> Int { events.filter { $0.kind == kind.rawValue }.count }

    func waitTerminal(_ seconds: Double = 5) -> Bool {
        terminalSignal.wait(timeout: .now() + seconds) == .success
    }

    /// Waits until at least `n` events of `kind` have arrived.
    func waitForCount(_ kind: sg_event_kind, _ n: Int, _ seconds: Double = 5) -> Bool {
        waitUntil(seconds) { $0.filter { $0.kind == kind.rawValue }.count >= n }
    }

    /// Waits until an event of `kind` with exactly `text` has arrived.
    func waitForText(_ kind: sg_event_kind, _ text: String, _ seconds: Double = 5) -> Bool {
        waitUntil(seconds) { $0.contains { $0.kind == kind.rawValue && $0.text == text } }
    }

    private func waitUntil(_ seconds: Double, _ done: ([SeenEvent]) -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if done(events) { return true }
            _ = anySignal.wait(timeout: .now() + 0.05)
        }
        return done(events)
    }

    /// Waits until an event of `kind` (with non-empty text for PARTIAL) has arrived.
    func waitFor(_ kind: sg_event_kind, _ seconds: Double = 5) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if events.contains(where: { $0.kind == kind.rawValue && (kind != SG_EV_PARTIAL || !$0.text.isEmpty) }) {
                return true
            }
            _ = anySignal.wait(timeout: .now() + 0.05)
        }
        return false
    }
}

/// sg_speech_start with a Recorder as the context (the closure captures nothing, so it converts
/// to the C function pointer type sg_speech_cb).
func startSession(_ rec: Recorder, locale: String = "en-US", rate: Double = 48_000) -> OpaquePointer? {
    locale.withCString { loc in
        sg_speech_start(loc, rate, { ctx, kind, text, a0, a1, code in
            guard let ctx else { return }
            Unmanaged<Recorder>.fromOpaque(ctx).takeUnretainedValue().receive(kind.rawValue, text, a0, a1, code)
        }, rec.context)
    }
}

/// A sine tone.
func tone(seconds: Double, rate: Double, hz: Double = 440, amplitude: Float = 0.5) -> [Float] {
    let n = Int((seconds * rate).rounded())
    return (0..<n).map { amplitude * Float(sin(2 * Double.pi * hz * Double($0) / rate)) }
}

/// Pushes `samples` in chunks; returns the codes sg_speech_push_f32 returned that were not OK.
@discardableResult
func pushAll(_ s: OpaquePointer, _ samples: [Float], chunk: Int = 480) -> [Int32] {
    var bad: [Int32] = []
    samples.withUnsafeBufferPointer { b in
        var o = 0
        while o < b.count {
            let n = min(chunk, b.count - o)
            let rc = sg_speech_push_f32(s, b.baseAddress! + o, n)
            if rc != SG_SPEECH_OK { bad.append(rc) }
            o += n
        }
    }
    return bad
}

/// Frequency of a tone from its zero crossings.
func zeroCrossingHz(_ x: ArraySlice<Float>, rate: Double) -> Double {
    var crossings = 0
    var prev = x.first ?? 0
    for v in x.dropFirst() {
        if (prev < 0 && v >= 0) || (prev >= 0 && v < 0) { crossings += 1 }
        prev = v
    }
    return Double(crossings) / 2 / (Double(x.count) / rate)
}

func rms(_ x: ArraySlice<Float>) -> Double {
    guard !x.isEmpty else { return 0 }
    return sqrt(x.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(x.count))
}

/// The package's root folder (app/speech-macos), from this file's location.
let packageRoot: URL = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

/// Base class: every test starts and ends with the trap engine and the fake status probe.
class SpeechTestCase: XCTestCase {
    override func setUp() {
        super.setUp()
        MockInstall.reset()
    }

    override func tearDown() {
        MockInstall.reset()
        super.tearDown()
    }
}
