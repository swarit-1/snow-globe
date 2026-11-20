import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import XCTest

/// Argument checks and marshaling at the C boundary (include/lookout_speech.h; PRD §6.5). Every
/// call here goes through the header's declarations to the @_cdecl symbols, as the Rust shell's do.
final class ABITests: SpeechTestCase {
    func testStartRejectsBadArgumentsWithNULL() {
        MockInstall.mock()
        let rec = Recorder()
        let cb: lk_speech_cb = { _, _, _, _, _, _ in }
        XCTAssertNil(lk_speech_start(nil, 48_000, cb, nil), "NULL locale")
        for bad in ["", "e", "en US", "en-US!", String(repeating: "a", count: 65), "ünï"] {
            XCTAssertNil(bad.withCString { lk_speech_start($0, 48_000, cb, nil) }, "locale '\(bad)'")
        }
        var invalidUTF8: [CChar] = [0x65, 0x6E, CChar(bitPattern: 0xFF), 0]
        XCTAssertNil(lk_speech_start(&invalidUTF8, 48_000, cb, nil), "invalid UTF-8")
        for rate in [0, -1, 7_999, 384_001, .nan, .infinity, -.infinity] as [Double] {
            XCTAssertNil(lk_speech_start("en-US", rate, cb, nil), "rate \(rate)")
        }
        XCTAssertNil(lk_speech_start("en-US", 48_000, nil, rec.context), "NULL callback")
        XCTAssertTrue(rec.events.isEmpty)
    }

    func testStartAcceptsTheEdgesOfTheRanges() throws {
        MockInstall.mock()
        for (loc, rate) in [("en-US", 8_000.0), ("zh-Hant-TW", 384_000.0), ("en_GB", 44_100.0), ("de", 16_000.0)] {
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec, locale: loc, rate: rate), "\(loc) at \(rate)")
            lk_speech_free(s)
        }
    }

    func testNullSessionCallsAreHarmless() {
        var x: Float = 0
        XCTAssertEqual(lk_speech_push_f32(nil, &x, 1), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(lk_speech_finish(nil), LK_SPEECH_ERR_ARGUMENT)
        lk_speech_cancel(nil)
        lk_speech_free(nil)
    }

    func testPushArgumentRules() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        XCTAssertEqual(lk_speech_push_f32(s, nil, 0), LK_SPEECH_OK, "zero frames is a no-op, even with NULL")
        XCTAssertEqual(lk_speech_push_f32(s, nil, 10), LK_SPEECH_ERR_ARGUMENT)
        var one: Float = 0.25
        XCTAssertEqual(lk_speech_push_f32(s, &one, 1), LK_SPEECH_OK)
        lk_speech_free(s)
    }

    func testPushCopiesTheCallersSamples() throws {
        // Same rate in and out, Float32: the converter passes samples through unchanged.
        var script = MockEngine.Script()
        script.commonFormat = .pcmFormatFloat32
        script.sampleRate = 16_000
        let box = MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec, rate: 16_000))
        var buf = [Float](repeating: 0, count: 160)
        for k in 0..<10 {
            for i in 0..<160 { buf[i] = Float(k * 160 + i) / 10_000 }
            XCTAssertEqual(lk_speech_push_f32(s, buf, 160), LK_SPEECH_OK)
            // The caller reuses its buffer at once, as an audio callback does.
            for i in 0..<160 { buf[i] = -9 }
        }
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        lk_speech_free(s)
        let fed = try XCTUnwrap(box.last?.stats)
        XCTAssertEqual(fed.samples.count, 1_600)
        for i in 0..<1_600 { XCTAssertEqual(fed.samples[i], Float(i) / 10_000, accuracy: 1e-7) }
    }

    func testVersionIsStaticAndDescribesTheBuild() {
        let a = lk_speech_version()
        let b = lk_speech_version()
        XCTAssertEqual(a, b, "the same static string every time")
        let v = String(cString: a!)
        XCTAssertTrue(v.hasPrefix("lookout-speech 0.2.0 "), v)
        XCTAssertTrue(v.contains("DictationTranscriber"), v)
        XCTAssertTrue(v.contains("on device only"), v)
        XCTAssertNotNil(v.range(of: #"macOS SDK \d{6}"#, options: .regularExpression), v)
    }

    func testContextualStringsSetterValidatesAndCopies() {
        XCTAssertEqual(setContextualStrings(["Mount Rainier", "Grand Canyon"]), LK_SPEECH_OK)
        XCTAssertEqual(Settings.snapshot().contextualStrings, ["Mount Rainier", "Grand Canyon"])
        XCTAssertEqual(lk_speech_set_contextual_strings(nil, 1), LK_SPEECH_ERR_ARGUMENT)
        let nulls: [UnsafePointer<CChar>?] = [nil]
        XCTAssertEqual(nulls.withUnsafeBufferPointer { lk_speech_set_contextual_strings($0.baseAddress, 1) }, LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(setContextualStrings(["ok", "  "]), LK_SPEECH_ERR_ARGUMENT, "blank string")
        let bad: [CChar] = [0x41, CChar(bitPattern: 0xC3), 0]
        let rc = bad.withUnsafeBufferPointer { b -> Int32 in
            let list: [UnsafePointer<CChar>?] = [b.baseAddress]
            return list.withUnsafeBufferPointer { lk_speech_set_contextual_strings($0.baseAddress, 1) }
        }
        XCTAssertEqual(rc, LK_SPEECH_ERR_ARGUMENT, "invalid UTF-8")
        XCTAssertEqual(setContextualStrings((0...1_000).map { "place \($0)" }), LK_SPEECH_ERR_ARGUMENT, "1,001 strings")
        XCTAssertEqual(Settings.snapshot().contextualStrings, ["Mount Rainier", "Grand Canyon"], "a rejected call changes nothing")
        XCTAssertEqual(lk_speech_set_contextual_strings(nil, 0), LK_SPEECH_OK, "count 0 clears")
        XCTAssertEqual(Settings.snapshot().contextualStrings, [])
        XCTAssertEqual(setContextualStrings((0..<1_000).map { "place \($0)" }), LK_SPEECH_OK)
    }

    func testPadAndRetentionSettersValidate() {
        XCTAssertEqual(lk_speech_set_silence_pad_ms(-1), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(lk_speech_set_silence_pad_ms(2_001), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(lk_speech_set_silence_pad_ms(2_000), LK_SPEECH_OK)
        XCTAssertEqual(lk_speech_set_silence_pad_ms(0), LK_SPEECH_OK)
        XCTAssertEqual(Settings.snapshot().silencePadMs, 0)
        XCTAssertEqual(lk_speech_set_model_retention(-1), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(lk_speech_set_model_retention(3), LK_SPEECH_ERR_ARGUMENT)
        for (c, r) in [(LK_SPEECH_RETAIN_WHILE_IN_USE, ModelRetention.whileInUse), (LK_SPEECH_RETAIN_LINGERING, .lingering), (LK_SPEECH_RETAIN_PROCESS_LIFETIME, .processLifetime)] {
            XCTAssertEqual(lk_speech_set_model_retention(c), LK_SPEECH_OK)
            XCTAssertEqual(Settings.snapshot().retention, r)
        }
    }

    func testDefaultsAreTheDocumentedOnes() {
        let d = SessionSettings()
        XCTAssertEqual(d.silencePadMs, 0)
        XCTAssertEqual(d.retention, .lingering)
        XCTAssertEqual(d.contextualStrings, [])
        XCTAssertEqual(d.finishTimeoutSeconds, 15)
        XCTAssertEqual(statusTimeout.withLock { $0 }, 10)
    }

    func testStatusGoesThroughTheProbeAndMapsItsAnswer() {
        let cases: [(StatusFacts, Int32)] = [
            (StatusFacts(localeSupported: true, authorization: .notDetermined, assets: .installed), LK_SPEECH_OK),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .installed), LK_SPEECH_OK),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .supported), LK_SPEECH_ASSET_NOT_INSTALLED),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .downloading), LK_SPEECH_ASSET_NOT_INSTALLED),
            (StatusFacts(localeSupported: true, authorization: .denied, assets: .installed), LK_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: true, authorization: .restricted, assets: .supported), LK_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: true, authorization: .unknown, assets: .installed), LK_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: false, authorization: .denied, assets: .unsupported), LK_SPEECH_LOCALE_UNSUPPORTED),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .unsupported), LK_SPEECH_LOCALE_UNSUPPORTED),
        ]
        for (facts, want) in cases {
            StatusProbes.current.withLock { $0 = FakeStatusProbe(facts) }
            XCTAssertEqual(lk_speech_status("en-US"), want, "\(facts)")
            XCTAssertEqual(StatusMapping.code(facts), want)
        }
        XCTAssertEqual(lk_speech_status(nil), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(lk_speech_status("not a locale"), LK_SPEECH_ERR_ARGUMENT)
    }

    func testStatusTimesOutInsteadOfBlockingForever() {
        StatusProbes.current.withLock {
            $0 = FakeStatusProbe(StatusFacts(localeSupported: true, authorization: .authorized, assets: .installed), delaySeconds: 2)
        }
        statusTimeout.withLock { $0 = 0.2 }
        let t0 = Date()
        XCTAssertEqual(lk_speech_status("en-US"), LK_SPEECH_ERR_TIMEOUT)
        XCTAssertLessThan(Date().timeIntervalSince(t0), 1.5)
    }

    /// A compile-time check: each Swift entry point is assigned to a variable whose type is taken
    /// from the header's declaration (not restated), so this file stops compiling when a
    /// prototype in include/lookout_speech.h and its @_cdecl implementation in CABI.swift differ
    /// in the number, order or types of parameters or in the result. Swift imports the C
    /// declaration as a Swift function type, so the one difference it lets through is
    /// nullability (an Optional pointer where the header says non-null), which does not change
    /// the C ABI. The library's own build does not catch drift (the @_cdecl functions are not
    /// checked against the header). The calls at the end run the bound functions on argument
    /// checks that need no session.
    func testEntryPointsHaveTheHeadersExactTypes() {
        var status = lk_speech_status
        status = lkSpeechStatusEntry
        var start = lk_speech_start
        start = lkSpeechStartEntry
        var push = lk_speech_push_f32
        push = lkSpeechPushF32Entry
        var finish = lk_speech_finish
        finish = lkSpeechFinishEntry
        var cancel = lk_speech_cancel
        cancel = lkSpeechCancelEntry
        var release = lk_speech_free
        release = lkSpeechFreeEntry
        var contextual = lk_speech_set_contextual_strings
        contextual = lkSpeechSetContextualStringsEntry
        var pad = lk_speech_set_silence_pad_ms
        pad = lkSpeechSetSilencePadMsEntry
        var retention = lk_speech_set_model_retention
        retention = lkSpeechSetModelRetentionEntry
        XCTAssertEqual(status(nil), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertNil(start(nil, 48_000, nil, nil))
        XCTAssertEqual(push(nil, nil, 1), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(finish(nil), LK_SPEECH_ERR_ARGUMENT)
        cancel(nil)
        release(nil)
        XCTAssertEqual(contextual(nil, 0), LK_SPEECH_OK)
        XCTAssertEqual(pad(-1), LK_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(retention(99), LK_SPEECH_ERR_ARGUMENT)
        print("entry point type: \(type(of: status))")
    }

    func testHeaderCodesAreTheDocumentedValues() {
        XCTAssertEqual(LK_EV_READY.rawValue, 0)
        XCTAssertEqual(LK_EV_PARTIAL.rawValue, 1)
        XCTAssertEqual(LK_EV_FINAL.rawValue, 2)
        XCTAssertEqual(LK_EV_ERROR.rawValue, 3)
        XCTAssertEqual(LK_EV_STABLE.rawValue, 4)
        XCTAssertEqual(MemoryLayout<lk_event_kind>.size, 4)
        XCTAssertEqual(
            [LK_SPEECH_OK, LK_SPEECH_ASSET_NOT_INSTALLED, LK_SPEECH_NOT_AUTHORIZED, LK_SPEECH_LOCALE_UNSUPPORTED],
            [0, 1, 2, 3])
        XCTAssertEqual(
            [LK_SPEECH_ERR_ARGUMENT, LK_SPEECH_ERR_STATE, LK_SPEECH_ERR_OVERFLOW, LK_SPEECH_ERR_AUDIO_FORMAT,
             LK_SPEECH_ERR_ENGINE, LK_SPEECH_ERR_TIMEOUT, LK_SPEECH_ERR_RESOURCES, LK_SPEECH_ERR_ASSET_RESERVATION,
             LK_SPEECH_ERR_INTERNAL],
            [-1, -2, -3, -4, -5, -6, -7, -8, -9])
        XCTAssertEqual(
            [LK_SPEECH_RETAIN_WHILE_IN_USE, LK_SPEECH_RETAIN_LINGERING, LK_SPEECH_RETAIN_PROCESS_LIFETIME], [0, 1, 2])
        XCTAssertEqual(ModelRetention.allCases.map(\.rawValue), [0, 1, 2])
    }
}
