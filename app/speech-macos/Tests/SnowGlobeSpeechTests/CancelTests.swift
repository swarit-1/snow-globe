import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import Synchronization
import XCTest

/// `sg_speech_cancel`: no callback after it returns, from any thread, at any point of a session
/// (PRD §10.2: a new key press cancels everything in flight).
final class CancelTests: SpeechTestCase {
    func testNoCallbackAfterCancelReturnsUnderRandomTiming() throws {
        var fastScript = MockEngine.Script()
        fastScript.secondsPerWord = 0.01
        fastScript.finalizeEverySeconds = 0.05
        var queuedScript = fastScript
        queuedScript.resultsOnFeedThread = false
        var slowSetup = fastScript
        slowSetup.prepareDelaySeconds = 0.01
        let scripts = [fastScript, queuedScript, slowSetup]
        var rng = SystemRandomNumberGenerator()
        let iterations = 300
        var totalEvents = 0
        var cancelledWithFinishPending = 0
        for i in 0..<iterations {
            MockInstall.mock(scripts[i % scripts.count])
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec))
            let audio = tone(seconds: 0.3, rate: 48_000)
            // Push from another thread, as the audio thread would, while this thread cancels.
            let pusherDone = DispatchSemaphore(value: 0)
            let sp = SessionPointer(s)
            Thread.detachNewThread {
                audio.withUnsafeBufferPointer { b in
                    var o = 0
                    while o < b.count {
                        let n = min(480, b.count - o)
                        _ = sg_speech_push_f32(sp.raw, b.baseAddress! + o, n)
                        o += n
                    }
                }
                pusherDone.signal()
            }
            usleep(UInt32.random(in: 0...15_000, using: &rng))
            if Bool.random(using: &rng) {
                _ = sg_speech_finish(s)
                cancelledWithFinishPending += 1
                usleep(UInt32.random(in: 0...3_000, using: &rng))
            }
            sg_speech_cancel(s)
            rec.cancelReturned.store(true, ordering: .releasing)
            pusherDone.wait()
            usleep(UInt32.random(in: 0...2_000, using: &rng))
            sg_speech_free(s)
            usleep(2_000)
            XCTAssertEqual(rec.violations.load(ordering: .acquiring), 0, "iteration \(i)")
            XCTAssertFalse(rec.events.contains(where: \.afterCancelReturned), "iteration \(i)")
            XCTAssertLessThanOrEqual(rec.count(SG_EV_FINAL) + rec.count(SG_EV_ERROR), 1, "iteration \(i)")
            totalEvents += rec.events.count
        }
        // The stress has to exercise callbacks, not just cancel idle sessions.
        XCTAssertGreaterThan(totalEvents, iterations)
        XCTAssertGreaterThan(cancelledWithFinishPending, iterations / 4)
        print("cancel stress: \(iterations) sessions, \(totalEvents) callbacks before cancel, 0 after")
    }

    func testCancelFromInsideACallbackDoesNotDeadlock() throws {
        var script = MockEngine.Script()
        script.secondsPerWord = 0.02
        MockInstall.mock(script)
        let rec = Recorder()
        let holder = SessionHolder()
        rec.hook = { e in
            if e.kind == SG_EV_PARTIAL.rawValue, let s = holder.session {
                sg_speech_cancel(s)
                rec.cancelReturned.store(true, ordering: .releasing)
            }
        }
        let s = try XCTUnwrap(startSession(rec))
        holder.session = s
        pushAll(s, tone(seconds: 1.0, rate: 48_000))
        XCTAssertTrue(rec.waitFor(SG_EV_PARTIAL))
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_ERR_STATE, "cancelled sessions don't finish")
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_PARTIAL), 1, "the cancelling callback was the last one")
        XCTAssertEqual(rec.violations.load(ordering: .acquiring), 0)
    }

    func testFreeFromInsideTheFinalCallback() throws {
        MockInstall.mock()
        let rec = Recorder()
        let holder = SessionHolder()
        let freed = DispatchSemaphore(value: 0)
        rec.hook = { e in
            if e.kind == SG_EV_FINAL.rawValue, let s = holder.session {
                sg_speech_free(s)
                freed.signal()
            }
        }
        let s = try XCTUnwrap(startSession(rec))
        holder.session = s
        pushAll(s, tone(seconds: 0.5, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertEqual(freed.wait(timeout: .now() + 5), .success)
        Thread.sleep(forTimeInterval: 0.05)
        XCTAssertEqual(rec.events.last?.kind, SG_EV_FINAL.rawValue)
    }

    func testFreeWithoutFinishOrCancelStopsEverything() throws {
        var script = MockEngine.Script()
        script.secondsPerWord = 0.01
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.5, rate: 48_000))
        XCTAssertTrue(rec.waitFor(SG_EV_PARTIAL))
        sg_speech_free(s)
        rec.cancelReturned.store(true, ordering: .releasing)
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(rec.violations.load(ordering: .acquiring), 0)
        XCTAssertEqual(rec.count(SG_EV_FINAL) + rec.count(SG_EV_ERROR), 0, "no terminal event without finish")
        XCTAssertEqual(box.last?.stats.cancelCalls, 1, "the recognizer was told to stop")
        XCTAssertEqual(box.last?.stats.violations, [])
    }

    func testCancelIsIdempotentAndWorksAfterTheTerminalEvent() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.3, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_cancel(s)
        sg_speech_cancel(s)
        XCTAssertEqual(sg_speech_push_f32(s, [0.1], 1), SG_SPEECH_ERR_STATE)
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 1)
    }

    func testCancelBeforeSetupEndsMeansNoReady() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 0.2
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.2, rate: 48_000))
        sg_speech_cancel(s)
        rec.cancelReturned.store(true, ordering: .releasing)
        Thread.sleep(forTimeInterval: 0.4)
        sg_speech_free(s)
        XCTAssertTrue(rec.events.isEmpty)
        XCTAssertEqual(box.last?.stats.framesFed, 0, "no audio reached the recognizer")
    }

    func testCloseZeroesTheRing() {
        let ring = FloatRing(minimumCapacity: 1_024)
        let x = [Float](repeating: 0.7, count: 600)
        XCTAssertTrue(x.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: 600) })
        XCTAssertFalse(ring.storageIsAllZero())
        ring.zeroAll()
        XCTAssertTrue(ring.storageIsAllZero())
    }
}

/// Passes a session pointer to another thread in a test (the C ABI allows pushes from one other
/// thread while this one controls the session).
struct SessionPointer: @unchecked Sendable {
    let raw: OpaquePointer
    init(_ r: OpaquePointer) { raw = r }
}

final class SessionHolder: @unchecked Sendable {
    var session: OpaquePointer?
}
