import Darwin
import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import Synchronization
import XCTest

/// More of `sg_speech_cancel` and `sg_speech_free` (include/snow_globe_speech.h; PRD §10.2: a new key
/// press cancels everything in flight).
final class CancelEdgeTests: SpeechTestCase {
    /// "No audio reaches the recognizer after it returns": the mock counts every feed that starts
    /// after the test raised its fence, which it does the moment sg_speech_cancel returns.
    func testNoAudioReachesTheRecognizerAfterCancelReturnsUnderRandomTiming() throws {
        var rng = SystemRandomNumberGenerator()
        let iterations = 200
        var sessionsThatFed = 0
        var withPad = 0
        for i in 0..<iterations {
            var script = MockEngine.Script()
            script.secondsPerWord = 0.01
            script.resultsOnFeedThread = i % 2 == 0
            let box = MockInstall.mock(script)
            let pad: Int32 = i % 3 == 0 ? 500 : 0
            XCTAssertEqual(sg_speech_set_silence_pad_ms(pad), SG_SPEECH_OK)
            if pad > 0 { withPad += 1 }
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec))
            let engine = try XCTUnwrap(box.last)
            let audio = tone(seconds: 0.3, rate: 48_000)
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
                usleep(UInt32.random(in: 0...3_000, using: &rng))
            }
            sg_speech_cancel(s)
            engine.fence.store(true, ordering: .releasing)
            pusherDone.wait()
            usleep(UInt32.random(in: 0...5_000, using: &rng))
            sg_speech_free(s)
            usleep(3_000)
            let st = engine.stats
            XCTAssertEqual(st.feedsAfterFence, 0, "iteration \(i): audio reached the recognizer after cancel returned")
            XCTAssertEqual(st.violations, [], "iteration \(i)")
            if st.buffersFed > 0 { sessionsThatFed += 1 }
        }
        XCTAssertGreaterThan(sessionsThatFed, iterations / 4, "the stress has to cancel sessions that were feeding")
        XCTAssertGreaterThan(withPad, 0)
        print("cancel feed fence: \(iterations) sessions (\(sessionsThatFed) were feeding, \(withPad) with a pad), 0 feeds after cancel")
    }

    func testFreeFromInsideTheReadyCallback() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 0.1
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let holder = SessionHolder()
        let freed = DispatchSemaphore(value: 0)
        rec.hook = { e in
            if e.kind == SG_EV_READY.rawValue, let s = holder.session {
                sg_speech_free(s)
                freed.signal()
            }
        }
        let s = try XCTUnwrap(startSession(rec))
        holder.session = s
        // Pushed while setup sleeps, so no call on the session overlaps the free.
        XCTAssertEqual(pushAll(s, tone(seconds: 0.05, rate: 48_000)), [])
        XCTAssertEqual(freed.wait(timeout: .now() + 5), .success)
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(rec.events.map(\.kind), [SG_EV_READY.rawValue], "nothing after the callback that freed it")
        XCTAssertEqual(box.last?.stats.cancelCalls, 1)
        XCTAssertEqual(box.last?.stats.violations, [])
    }

    func testCancelWhileTheRecognizerIsFinishingSendsNoTerminalEvent() throws {
        var script = MockEngine.Script()
        script.finishDelaySeconds = 0.3
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.3, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        let deadline = Date().addingTimeInterval(2)
        while box.last?.stats.finishCalls != 1, Date() < deadline { usleep(1_000) }
        XCTAssertEqual(box.last?.stats.finishCalls, 1, "the recognizer is finishing")
        sg_speech_cancel(s)
        rec.cancelReturned.store(true, ordering: .releasing)
        Thread.sleep(forTimeInterval: 0.5)
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_FINAL) + rec.count(SG_EV_ERROR), 0)
        XCTAssertEqual(rec.violations.load(ordering: .acquiring), 0)
        XCTAssertEqual(box.last?.stats.cancelCalls, 1)
    }

    /// The header: the first sg_speech_cancel waits for the session's current audio step, one
    /// chunk of at most 4,096 samples. Measured while a 19 s backlog drains (real conversion,
    /// mock recognizer), and while a 2 s silence pad is fed to a recognizer that takes 10 ms per
    /// buffer (without the pad's cancel check, cancel would wait for the rest of the pad).
    func testCancelWaitsForOneChunkAtMost() throws {
        var backlogMs: [Double] = []
        for _ in 0..<5 {
            var script = MockEngine.Script()
            script.prepareDelaySeconds = 0.3
            MockInstall.mock(script)
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec))
            XCTAssertEqual(pushAll(s, tone(seconds: 19, rate: 48_000), chunk: 4_800), [])
            XCTAssertTrue(rec.waitFor(SG_EV_READY))
            usleep(2_000)  // the drain of the backlog is under way
            let t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            sg_speech_cancel(s)
            let t1 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            backlogMs.append(Double(t1 - t0) / 1e6)
            sg_speech_free(s)
        }

        var padMs: [Double] = []
        var padFed: [Int] = []
        for _ in 0..<3 {
            var script = MockEngine.Script()
            script.feedDelaySeconds = 0.010
            let box = MockInstall.mock(script)
            XCTAssertEqual(sg_speech_set_silence_pad_ms(2_000), SG_SPEECH_OK)
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec))
            pushAll(s, tone(seconds: 0.2, rate: 48_000))
            XCTAssertTrue(rec.waitFor(SG_EV_READY))
            Thread.sleep(forTimeInterval: 0.08)  // the 0.2 s of audio has been fed
            XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
            Thread.sleep(forTimeInterval: 0.06)  // inside the pad: 24 chunks of 10 ms each
            let t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            sg_speech_cancel(s)
            let t1 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            padMs.append(Double(t1 - t0) / 1e6)
            sg_speech_free(s)
            padFed.append(box.last?.stats.framesFed ?? -1)
            XCTAssertEqual(rec.count(SG_EV_FINAL) + rec.count(SG_EV_ERROR), 0)
        }
        print(String(
            format: "cancel wait (ms): during a 19 s backlog drain max %.3f (of %@); during a 2 s pad at 10 ms per buffer max %.3f (of %@)",
            backlogMs.max() ?? -1, backlogMs.map { String(format: "%.3f", $0) }.joined(separator: " "),
            padMs.max() ?? -1, padMs.map { String(format: "%.3f", $0) }.joined(separator: " ")))
        XCTAssertLessThan(backlogMs.max() ?? .infinity, 50)
        XCTAssertLessThan(padMs.max() ?? .infinity, 60, "one 10 ms buffer, not the rest of a 240 ms pad")
        // 0.2 s of audio plus the part of the pad fed before the cancel: far less than the full pad.
        for f in padFed { XCTAssertLessThan(f, Int(1.2 * 16_000), "the pad stopped at the cancel (fed \(f))") }
    }
}
