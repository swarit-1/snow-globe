import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import XCTest

/// Edge cases of the session lifecycle (include/lookout_speech.h, SESSION LIFECYCLE; PRD §7.1).
final class LifecycleEdgeTests: SpeechTestCase {
    func testAStartFailureIsOneErrorAndFinishThenReturnsErrState() throws {
        var script = MockEngine.Script()
        script.startError = EngineError(LK_SPEECH_ERR_RESOURCES, "mock: too many analyses")
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.2, rate: 48_000))
        XCTAssertTrue(rec.waitTerminal())
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_ERR_STATE)
        Thread.sleep(forTimeInterval: 0.1)
        lk_speech_free(s)
        XCTAssertEqual(rec.events.map(\.kind), [LK_EV_ERROR.rawValue], "no READY: the recognizer never ran")
        XCTAssertEqual(rec.events.first?.code, LK_SPEECH_ERR_RESOURCES)
        XCTAssertEqual(rec.events.first?.text, "mock: too many analyses")
        let st = try XCTUnwrap(box.last?.stats)
        XCTAssertEqual(st.startCalls, 1)
        XCTAssertEqual(st.framesFed, 0)
        XCTAssertEqual(st.cancelCalls, 1, "the failed recognizer is released")
    }

    func testTheWatchdogCanFireDuringALongSetupAndNothingFollowsIt() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 1.0
        let box = MockInstall.mock(script)
        Settings.current.withLock { $0.finishTimeoutSeconds = 0.3 }
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.3, rate: 48_000))
        let t0 = Date()
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal(5))
        XCTAssertLessThan(Date().timeIntervalSince(t0), 0.9, "the timeout came before setup ended")
        // Setup completes about 0.7 s later: no READY, no audio to the recognizer.
        Thread.sleep(forTimeInterval: 1.2)
        lk_speech_free(s)
        XCTAssertEqual(rec.events.map(\.kind), [LK_EV_ERROR.rawValue])
        XCTAssertEqual(rec.events.first?.code, LK_SPEECH_ERR_TIMEOUT)
        XCTAssertEqual(box.last?.stats.framesFed, 0)
        XCTAssertEqual(box.last?.stats.violations, [])
    }

    func testOverflowIsStickyEvenWhenTheDropCountWouldWrap() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        var x: Float = 0.1
        // Two refused pushes whose frame counts add past Int.max (a caller passing garbage).
        XCTAssertEqual(lk_speech_push_f32(s, &x, Int.max), LK_SPEECH_ERR_OVERFLOW)
        XCTAssertEqual(lk_speech_push_f32(s, &x, Int.max), LK_SPEECH_ERR_OVERFLOW)
        XCTAssertEqual(pushAll(s, tone(seconds: 0.3, rate: 48_000)), [], "later pushes fit")
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        lk_speech_free(s)
        XCTAssertEqual(rec.count(LK_EV_FINAL), 0, "a session that dropped audio never ends in FINAL")
        let e = try XCTUnwrap(rec.events.last)
        XCTAssertEqual(e.kind, LK_EV_ERROR.rawValue)
        XCTAssertEqual(e.code, LK_SPEECH_ERR_OVERFLOW)
        XCTAssertTrue(e.text.hasPrefix("\(Int.max) frames were dropped"), "the count saturates: \(e.text)")
    }

    func testOneSmallRefusedPushIsEnoughForAnError() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 0.5
        MockInstall.mock(script)
        let rec = Recorder()
        // 8 kHz: the ring holds 2^18 frames (32.8 s). Fill it exactly, then one frame more.
        let s = try XCTUnwrap(startSession(rec, rate: 8_000))
        XCTAssertEqual(pushAll(s, [Float](repeating: 0.1, count: 262_144), chunk: 8_192), [])
        var x: Float = 0.1
        XCTAssertEqual(lk_speech_push_f32(s, &x, 1), LK_SPEECH_ERR_OVERFLOW)
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal(5))
        lk_speech_free(s)
        XCTAssertEqual(rec.events.last?.code, LK_SPEECH_ERR_OVERFLOW)
        XCTAssertTrue(rec.events.last?.text.hasPrefix("1 frames were dropped") ?? false)
    }
}
