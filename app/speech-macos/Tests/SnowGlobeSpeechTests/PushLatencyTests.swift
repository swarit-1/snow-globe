import Darwin
import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import Synchronization
import XCTest

/// How long `sg_speech_push_f32` holds its caller (the ABI: "must not block the caller (an audio
/// thread) for more than ~1 ms"). Measured with the host clock around each call, while the work
/// queue drains and converts concurrently, as in a live session.
///
/// The test thread is an ordinary thread, not CoreAudio's real-time one, so the scheduler can
/// preempt it in the middle of a push; such outliers are the scheduler's, not the push path's
/// (which takes no lock and makes no system call). The bound is therefore fixed before the run
/// as: at most 0.1 % of pushes over 1 ms. The measured numbers are printed either way.
final class PushLatencyTests: SpeechTestCase {
    func testPushStaysFarBelowOneMillisecond() throws {
        MockInstall.mock()
        let rec = Recorder()
        let rate = 48_000.0
        let s = try XCTUnwrap(startSession(rec, rate: rate))
        XCTAssertTrue(rec.waitFor(SG_EV_READY))
        let chunk = 480  // 10 ms, a typical microphone callback at 48 kHz
        let pushes = 5_000  // 50 s of audio, pushed at 10x real time (one push per millisecond)
        let samples = tone(seconds: Double(chunk) / rate, rate: rate)
        let sp = SessionPointer(s)
        let done = DispatchSemaphore(value: 0)
        let codes = Mutex<[Int32]>([])
        let measured = MeasuredBox()
        let thread = Thread {
            var local = [UInt64](repeating: 0, count: pushes)
            var bad: [Int32] = []
            samples.withUnsafeBufferPointer { b in
                for i in 0..<pushes {
                    let t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
                    let rc = sg_speech_push_f32(sp.raw, b.baseAddress!, chunk)
                    let t1 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
                    local[i] = t1 - t0
                    if rc != SG_SPEECH_OK { bad.append(rc) }
                    usleep(1_000)
                }
            }
            measured.set(local)
            codes.withLock { $0 = bad }
            done.signal()
        }
        thread.qualityOfService = .userInteractive
        thread.start()
        XCTAssertEqual(done.wait(timeout: .now() + 120), .success)
        let ns = measured.get()
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal(30))
        sg_speech_free(s)

        XCTAssertEqual(codes.withLock { $0 }, [], "every push was accepted")
        XCTAssertEqual(rec.events.last?.kind, SG_EV_FINAL.rawValue)
        let sorted = ns.sorted()
        func q(_ p: Double) -> Double { Double(sorted[min(sorted.count - 1, Int(p * Double(sorted.count - 1)))]) / 1_000 }
        let over = ns.filter { $0 > 1_000_000 }.count
        print(String(
            format: "push latency (%d pushes of %d frames, µs): p50 %.2f  p99 %.2f  p99.9 %.2f  max %.2f  over 1 ms: %d",
            pushes, chunk, q(0.5), q(0.99), q(0.999), Double(sorted.last ?? 0) / 1_000, over))
        XCTAssertLessThanOrEqual(over, pushes / 1_000, "at most 0.1 % of pushes over 1 ms")
        XCTAssertLessThan(q(0.99), 1_000)
    }
}

final class MeasuredBox: @unchecked Sendable {
    private let v = Mutex<[UInt64]>([])
    func set(_ x: [UInt64]) { v.withLock { $0 = x } }
    func get() -> [UInt64] { v.withLock { $0 } }
}
