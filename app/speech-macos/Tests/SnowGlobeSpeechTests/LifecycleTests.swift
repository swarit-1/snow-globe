import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import XCTest

/// Session lifecycle through the C ABI, with the scripted engine behind the seam (PRD §7.1
/// SpeechSession: partials while held, finish on key-up, cancel).
final class LifecycleTests: SpeechTestCase {
    private let fullText = "show me mount rainier at sunset tomorrow"

    func testHappyPathSendsReadyThenPartialsThenExactlyOneFinal() throws {
        let box = MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec, rate: 48_000))
        XCTAssertEqual(pushAll(s, tone(seconds: 1.5, rate: 48_000)), [])
        XCTAssertTrue(rec.waitFor(SG_EV_PARTIAL), "a partial arrives while the key is held")
        rec.finishCalled.store(true, ordering: .releasing)
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_cancel(s)
        sg_speech_free(s)
        rec.cancelReturned.store(true, ordering: .releasing)

        let ev = rec.events
        XCTAssertEqual(ev.first?.kind, SG_EV_READY.rawValue)
        XCTAssertEqual(rec.count(SG_EV_READY), 1)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 1)
        XCTAssertEqual(rec.count(SG_EV_ERROR), 0)
        XCTAssertEqual(ev.last?.kind, SG_EV_FINAL.rawValue, "nothing follows the terminal event")
        let final = try XCTUnwrap(ev.last)
        XCTAssertEqual(final.text, fullText)
        XCTAssertTrue(final.afterFinishCall)
        XCTAssertEqual(final.code, 0)
        XCTAssertEqual(final.start, 0, accuracy: 1e-9)
        XCTAssertEqual(final.end, 1.5, accuracy: 0.01, "the final's range ends with the audio")
        let partials = ev.filter { $0.kind == SG_EV_PARTIAL.rawValue }
        XCTAssertGreaterThanOrEqual(partials.count, 3)
        for p in partials {
            XCTAssertTrue(fullText.hasPrefix(p.text), "partial '\(p.text)' is a prefix of the transcript")
            XCTAssertEqual(p.code, 0)
            XCTAssertGreaterThanOrEqual(p.end, p.start)
        }
        let ends = partials.map(\.end)
        XCTAssertEqual(ends, ends.sorted(), "partials move forward in audio time")
        XCTAssertFalse(ev.contains { $0.onMainThread }, "callbacks never run on the caller's thread")
        XCTAssertEqual(rec.violations.load(ordering: .acquiring), 0)

        let m = try XCTUnwrap(box.last)
        let st = m.stats
        XCTAssertEqual(st.prepareCalls, 1)
        XCTAssertEqual(st.startCalls, 1)
        XCTAssertEqual(st.finishCalls, 1)
        XCTAssertEqual(st.violations, [])
        XCTAssertEqual(st.config?.locale, "en-US")
        XCTAssertEqual(st.config?.inputSampleRate, 48_000)
        XCTAssertEqual(st.config?.retention, .lingering)
        XCTAssertEqual(st.config?.contextualStrings, [])
        // 1.5 s at the mock's 16 kHz, give or take the resampler's edges.
        XCTAssertEqual(Double(st.framesFed), 1.5 * 16_000, accuracy: 64)
    }

    func testFinalNeverPrecedesFinishEvenWhenSegmentsFinalizeEarly() throws {
        var script = MockEngine.Script()
        script.finalizeEverySeconds = 0.2
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        // 1.1 s: five words (the mock adds one per 0.2 s; off the boundary, so rounding can't drop one).
        pushAll(s, tone(seconds: 1.1, rate: 48_000))
        XCTAssertTrue(rec.waitFor(SG_EV_PARTIAL))
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 0, "finalized segments are PARTIALs until finish")
        rec.finishCalled.store(true, ordering: .releasing)
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 1)
        XCTAssertTrue(rec.events.filter { $0.kind == SG_EV_FINAL.rawValue }.allSatisfy(\.afterFinishCall))
        XCTAssertEqual(rec.events.last?.text, "show me mount rainier at")
    }

    func testFinishBeforeTheRecognizerIsReadyStillTranscribesTheBufferedAudio() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 0.3
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 1.5, rate: 48_000))
        XCTAssertEqual(rec.count(SG_EV_READY), 0, "still setting up")
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        let ready = try XCTUnwrap(rec.events.first)
        XCTAssertEqual(ready.kind, SG_EV_READY.rawValue)
        XCTAssertEqual(ready.end, 1.5, accuracy: 0.001, "READY reports the audio buffered during setup")
        XCTAssertEqual(rec.events.last?.text, fullText)
        XCTAssertEqual(Double(box.last?.stats.framesFed ?? 0), 1.5 * 16_000, accuracy: 64)
    }

    func testSilenceAndShortSessionsStillEndInOneFinal() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, [Float](repeating: 0, count: 480))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 1)
        let f = try XCTUnwrap(rec.events.last)
        XCTAssertEqual(f.text, "")
        XCTAssertEqual(f.start, 0)
        XCTAssertEqual(f.end, 0)
        XCTAssertEqual(rec.count(SG_EV_PARTIAL), 0, "no empty partial before any text")
    }

    func testNonASCIITextArrivesAsUTF8() throws {
        var script = MockEngine.Script()
        script.words = ["café", "Point", "Reyes", "naïve", "Yavapai"]
        script.finalizeEverySeconds = nil
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 1.3, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(rec.events.last?.text, "café Point Reyes naïve Yavapai")
    }

    func testFinishTwiceAndPushAfterFinishAreRefused() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.5, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_ERR_STATE)
        XCTAssertEqual(pushAll(s, tone(seconds: 0.1, rate: 48_000)), [Int32](repeating: SG_SPEECH_ERR_STATE, count: 10))
        XCTAssertTrue(rec.waitTerminal())
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_ERR_STATE)
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 1)
    }

    func testSetupFailureIsOneErrorAndFinishThenSendsNothing() throws {
        var script = MockEngine.Script()
        script.prepareError = EngineError(SG_SPEECH_ASSET_NOT_INSTALLED, "mock: no model")
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        XCTAssertTrue(rec.waitTerminal())
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_ERR_STATE)
        XCTAssertEqual(pushAll(s, tone(seconds: 0.02, rate: 48_000)), [SG_SPEECH_ERR_STATE, SG_SPEECH_ERR_STATE])
        Thread.sleep(forTimeInterval: 0.05)
        sg_speech_free(s)
        let ev = rec.events
        XCTAssertEqual(ev.count, 1)
        XCTAssertEqual(ev.first?.kind, SG_EV_ERROR.rawValue)
        XCTAssertEqual(ev.first?.code, SG_SPEECH_ASSET_NOT_INSTALLED)
        XCTAssertEqual(ev.first?.text, "mock: no model")
    }

    func testSetupFailureAfterFinishIsStillTheOneTerminalEvent() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 0.2
        script.prepareError = EngineError(SG_SPEECH_LOCALE_UNSUPPORTED, "mock: no such locale")
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.3, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        Thread.sleep(forTimeInterval: 0.05)
        sg_speech_free(s)
        XCTAssertEqual(rec.events.map(\.kind), [SG_EV_ERROR.rawValue])
        XCTAssertEqual(rec.events.first?.code, SG_SPEECH_LOCALE_UNSUPPORTED)
        XCTAssertEqual(rec.events.first?.end ?? 0, 0.3, accuracy: 0.001, "ERROR reports the audio pushed")
    }

    func testRecognizerFailureMidUtteranceIsOneError() throws {
        var script = MockEngine.Script()
        script.failAfterSeconds = 0.5
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 1.0, rate: 48_000))
        XCTAssertTrue(rec.waitTerminal())
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_ERR_STATE)
        sg_speech_free(s)
        XCTAssertEqual(rec.count(SG_EV_ERROR), 1)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 0)
        XCTAssertEqual(rec.events.last?.kind, SG_EV_ERROR.rawValue)
        XCTAssertEqual(rec.events.last?.code, SG_SPEECH_ERR_ENGINE)
        XCTAssertGreaterThan(rec.count(SG_EV_PARTIAL), 0)
        Thread.sleep(forTimeInterval: 0.05)
        XCTAssertEqual(box.last?.stats.violations, [], "nothing is fed once the recognizer has failed")
    }

    func testFinishErrorFromTheRecognizerIsOneError() throws {
        var script = MockEngine.Script()
        script.finishError = EngineError(SG_SPEECH_ERR_RESOURCES, "mock: busy")
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.5, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(rec.events.last?.kind, SG_EV_ERROR.rawValue)
        XCTAssertEqual(rec.events.last?.code, SG_SPEECH_ERR_RESOURCES)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 0)
    }

    func testWatchdogTurnsAHungFinishIntoATimeoutError() throws {
        var script = MockEngine.Script()
        script.finishHangs = true
        let box = MockInstall.mock(script)
        Settings.current.withLock { $0.finishTimeoutSeconds = 0.3 }
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 0.5, rate: 48_000))
        let t0 = Date()
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal(5))
        let waited = Date().timeIntervalSince(t0)
        sg_speech_free(s)
        XCTAssertEqual(rec.events.last?.kind, SG_EV_ERROR.rawValue)
        XCTAssertEqual(rec.events.last?.code, SG_SPEECH_ERR_TIMEOUT)
        XCTAssertGreaterThanOrEqual(waited, 0.29)
        XCTAssertLessThan(waited, 2.0)
        Thread.sleep(forTimeInterval: 0.05)
        XCTAssertEqual(box.last?.stats.cancelCalls, 1, "the hung recognizer is cancelled, once")
    }

    func testOverflowDropsSamplesAndEndsInAnErrorNotAFinal() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 1.0
        MockInstall.mock(script)
        let rec = Recorder()
        // 8 kHz: the ring holds 2^18 = 262,144 frames (32.8 s); push 40 s while setup sleeps.
        let s = try XCTUnwrap(startSession(rec, rate: 8_000))
        let codes = pushAll(s, [Float](repeating: 0.1, count: 8_000 * 40), chunk: 8_000)
        XCTAssertTrue(codes.contains(SG_SPEECH_ERR_OVERFLOW))
        XCTAssertEqual(Set(codes), [SG_SPEECH_ERR_OVERFLOW])
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal(5))
        sg_speech_free(s)
        XCTAssertEqual(rec.events.last?.kind, SG_EV_ERROR.rawValue)
        XCTAssertEqual(rec.events.last?.code, SG_SPEECH_ERR_OVERFLOW)
        XCTAssertEqual(rec.count(SG_EV_FINAL), 0)
    }

    func testSilencePadIsPushedAsZerosAtKeyUp() throws {
        let box = MockInstall.mock()
        XCTAssertEqual(sg_speech_set_silence_pad_ms(300), SG_SPEECH_OK)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 1.0, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        let st = try XCTUnwrap(box.last?.stats)
        XCTAssertEqual(Double(st.framesFed), 1.3 * 16_000, accuracy: 64)
        let tail = st.samples.suffix(Int(0.25 * 16_000))
        XCTAssertLessThan(tail.map { abs($0) }.max() ?? 1, 1e-3, "the last 250 ms are the pad's silence")
        XCTAssertGreaterThan(rms(st.samples.prefix(16_000)), 0.3, "the tone came first")
    }

    func testNoPadByDefault() throws {
        let box = MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: 1.0, rate: 48_000))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(Double(box.last?.stats.framesFed ?? 0), 16_000, accuracy: 64)
    }

    func testSettingsReachTheRecognizerAndAreCopiedAtStart() throws {
        let box = MockInstall.mock()
        let names = ["Mount Rainier", "Point Reyes", "Yavapai Point"]
        XCTAssertEqual(setContextualStrings(names), SG_SPEECH_OK)
        XCTAssertEqual(sg_speech_set_model_retention(SG_SPEECH_RETAIN_PROCESS_LIFETIME), SG_SPEECH_OK)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec, locale: "en-GB", rate: 44_100))
        // Changing the settings now does not change the running session.
        XCTAssertEqual(setContextualStrings([]), SG_SPEECH_OK)
        pushAll(s, tone(seconds: 0.4, rate: 44_100))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        let cfg = try XCTUnwrap(box.last?.stats.config)
        XCTAssertEqual(cfg.contextualStrings, names)
        XCTAssertEqual(cfg.retention, .processLifetime)
        XCTAssertEqual(cfg.locale, "en-GB")
        XCTAssertEqual(cfg.inputSampleRate, 44_100)
    }

    func testConversionDeliversTheToneAtTheRecognizersRate() throws {
        let box = MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec, rate: 48_000))
        pushAll(s, tone(seconds: 1.0, rate: 48_000, hz: 440, amplitude: 0.5))
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        let st = try XCTUnwrap(box.last?.stats)
        XCTAssertEqual(st.formatSeen, "Int16 1 ch 16000.0 Hz")
        let middle = st.samples.dropFirst(1_600).prefix(12_800)
        XCTAssertEqual(zeroCrossingHz(middle, rate: 16_000), 440, accuracy: 2)
        XCTAssertEqual(rms(middle), 0.5 / sqrt(2), accuracy: 0.01)
    }

    func testTwoSessionsRunSideBySide() throws {
        MockInstall.mock()
        let a = Recorder()
        let b = Recorder()
        let sa = try XCTUnwrap(startSession(a, rate: 48_000))
        let sb = try XCTUnwrap(startSession(b, rate: 16_000))
        pushAll(sa, tone(seconds: 0.9, rate: 48_000))
        pushAll(sb, tone(seconds: 0.5, rate: 16_000), chunk: 160)
        XCTAssertEqual(sg_speech_finish(sa), SG_SPEECH_OK)
        XCTAssertEqual(sg_speech_finish(sb), SG_SPEECH_OK)
        XCTAssertTrue(a.waitTerminal())
        XCTAssertTrue(b.waitTerminal())
        sg_speech_free(sa)
        sg_speech_free(sb)
        XCTAssertEqual(a.events.last?.text, "show me mount rainier")
        XCTAssertEqual(b.events.last?.text, "show me")
    }

    func testTrapEngineIsWhatAnUnmockedTestGets() throws {
        let before = TrapEngine.uses.load(ordering: .acquiring)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        XCTAssertEqual(rec.events.last?.code, SG_SPEECH_ERR_INTERNAL)
        XCTAssertEqual(TrapEngine.uses.load(ordering: .acquiring), before + 1)
    }
}

func setContextualStrings(_ list: [String]) -> Int32 {
    let dup = list.map { strdup($0) }
    defer { dup.forEach { free($0) } }
    let ptrs: [UnsafePointer<CChar>?] = dup.map { $0.map { UnsafePointer($0) } }
    return ptrs.withUnsafeBufferPointer { sg_speech_set_contextual_strings($0.baseAddress, list.count) }
}

/// What `sg_speech_start` costs its caller at key-down (allocation and zeroing of the ring, the
/// two queues, the setup task). The spike opens the microphone only after it returns.
final class StartLatencyTests: SpeechTestCase {
    func testStartReturnsQuickly() throws {
        MockInstall.mock()
        var ns: [UInt64] = []
        for _ in 0..<50 {
            let rec = Recorder()
            let t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            let s = startSession(rec, rate: 48_000)
            let t1 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            ns.append(t1 - t0)
            sg_speech_free(try XCTUnwrap(s))
        }
        let sorted = ns.sorted()
        let p50 = Double(sorted[sorted.count / 2]) / 1e6
        let max = Double(sorted.last ?? 0) / 1e6
        print(String(format: "sg_speech_start at 48 kHz (50 calls, ms): p50 %.3f  max %.3f", p50, max))
        XCTAssertLessThan(p50, 5, "start does no recognizer work on the caller's thread")
    }
}
