import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import SnowGlobeSpeechMocks
import XCTest

/// Argument checks and marshaling at the C boundary (include/snow_globe_speech.h; PRD §6.5). Every
/// call here goes through the header's declarations to the @_cdecl symbols, as the Rust shell's do.
final class ABITests: SpeechTestCase {
    func testStartRejectsBadArgumentsWithNULL() {
        MockInstall.mock()
        let rec = Recorder()
        let cb: sg_speech_cb = { _, _, _, _, _, _ in }
        XCTAssertNil(sg_speech_start(nil, 48_000, cb, nil), "NULL locale")
        for bad in ["", "e", "en US", "en-US!", String(repeating: "a", count: 65), "ünï"] {
            XCTAssertNil(bad.withCString { sg_speech_start($0, 48_000, cb, nil) }, "locale '\(bad)'")
        }
        var invalidUTF8: [CChar] = [0x65, 0x6E, CChar(bitPattern: 0xFF), 0]
        XCTAssertNil(sg_speech_start(&invalidUTF8, 48_000, cb, nil), "invalid UTF-8")
        for rate in [0, -1, 7_999, 384_001, .nan, .infinity, -.infinity] as [Double] {
            XCTAssertNil(sg_speech_start("en-US", rate, cb, nil), "rate \(rate)")
        }
        XCTAssertNil(sg_speech_start("en-US", 48_000, nil, rec.context), "NULL callback")
        XCTAssertTrue(rec.events.isEmpty)
    }

    func testStartAcceptsTheEdgesOfTheRanges() throws {
        MockInstall.mock()
        for (loc, rate) in [("en-US", 8_000.0), ("zh-Hant-TW", 384_000.0), ("en_GB", 44_100.0), ("de", 16_000.0)] {
            let rec = Recorder()
            let s = try XCTUnwrap(startSession(rec, locale: loc, rate: rate), "\(loc) at \(rate)")
            sg_speech_free(s)
        }
    }

    func testNullSessionCallsAreHarmless() {
        var x: Float = 0
        XCTAssertEqual(sg_speech_push_f32(nil, &x, 1), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(sg_speech_finish(nil), SG_SPEECH_ERR_ARGUMENT)
        sg_speech_cancel(nil)
        sg_speech_free(nil)
    }

    func testPushArgumentRules() throws {
        MockInstall.mock()
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        XCTAssertEqual(sg_speech_push_f32(s, nil, 0), SG_SPEECH_OK, "zero frames is a no-op, even with NULL")
        XCTAssertEqual(sg_speech_push_f32(s, nil, 10), SG_SPEECH_ERR_ARGUMENT)
        var one: Float = 0.25
        XCTAssertEqual(sg_speech_push_f32(s, &one, 1), SG_SPEECH_OK)
        sg_speech_free(s)
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
            XCTAssertEqual(sg_speech_push_f32(s, buf, 160), SG_SPEECH_OK)
            // The caller reuses its buffer at once, as an audio callback does.
            for i in 0..<160 { buf[i] = -9 }
        }
        XCTAssertEqual(sg_speech_finish(s), SG_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        sg_speech_free(s)
        let fed = try XCTUnwrap(box.last?.stats)
        XCTAssertEqual(fed.samples.count, 1_600)
        for i in 0..<1_600 { XCTAssertEqual(fed.samples[i], Float(i) / 10_000, accuracy: 1e-7) }
    }

    func testVersionIsStaticAndDescribesTheBuild() {
        let a = sg_speech_version()
        let b = sg_speech_version()
        XCTAssertEqual(a, b, "the same static string every time")
        let v = String(cString: a!)
        XCTAssertTrue(v.hasPrefix("snow-globe-speech 0.3.0 "), v)
        XCTAssertTrue(v.contains("DictationTranscriber"), v)
        XCTAssertTrue(v.contains("on device only"), v)
        XCTAssertNotNil(v.range(of: #"macOS SDK \d{6}"#, options: .regularExpression), v)
    }

    func testContextualStringsSetterValidatesAndCopies() {
        XCTAssertEqual(setContextualStrings(["Mount Rainier", "Grand Canyon"]), SG_SPEECH_OK)
        XCTAssertEqual(Settings.snapshot().contextualStrings, ["Mount Rainier", "Grand Canyon"])
        XCTAssertEqual(sg_speech_set_contextual_strings(nil, 1), SG_SPEECH_ERR_ARGUMENT)
        let nulls: [UnsafePointer<CChar>?] = [nil]
        XCTAssertEqual(nulls.withUnsafeBufferPointer { sg_speech_set_contextual_strings($0.baseAddress, 1) }, SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(setContextualStrings(["ok", "  "]), SG_SPEECH_ERR_ARGUMENT, "blank string")
        let bad: [CChar] = [0x41, CChar(bitPattern: 0xC3), 0]
        let rc = bad.withUnsafeBufferPointer { b -> Int32 in
            let list: [UnsafePointer<CChar>?] = [b.baseAddress]
            return list.withUnsafeBufferPointer { sg_speech_set_contextual_strings($0.baseAddress, 1) }
        }
        XCTAssertEqual(rc, SG_SPEECH_ERR_ARGUMENT, "invalid UTF-8")
        XCTAssertEqual(setContextualStrings((0...1_000).map { "place \($0)" }), SG_SPEECH_ERR_ARGUMENT, "1,001 strings")
        XCTAssertEqual(Settings.snapshot().contextualStrings, ["Mount Rainier", "Grand Canyon"], "a rejected call changes nothing")
        XCTAssertEqual(sg_speech_set_contextual_strings(nil, 0), SG_SPEECH_OK, "count 0 clears")
        XCTAssertEqual(Settings.snapshot().contextualStrings, [])
        XCTAssertEqual(setContextualStrings((0..<1_000).map { "place \($0)" }), SG_SPEECH_OK)
    }

    func testPadAndRetentionSettersValidate() {
        XCTAssertEqual(sg_speech_set_silence_pad_ms(-1), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(sg_speech_set_silence_pad_ms(2_001), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(sg_speech_set_silence_pad_ms(2_000), SG_SPEECH_OK)
        XCTAssertEqual(sg_speech_set_silence_pad_ms(0), SG_SPEECH_OK)
        XCTAssertEqual(Settings.snapshot().silencePadMs, 0)
        XCTAssertEqual(sg_speech_set_model_retention(-1), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(sg_speech_set_model_retention(3), SG_SPEECH_ERR_ARGUMENT)
        for (c, r) in [(SG_SPEECH_RETAIN_WHILE_IN_USE, ModelRetention.whileInUse), (SG_SPEECH_RETAIN_LINGERING, .lingering), (SG_SPEECH_RETAIN_PROCESS_LIFETIME, .processLifetime)] {
            XCTAssertEqual(sg_speech_set_model_retention(c), SG_SPEECH_OK)
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
            (StatusFacts(localeSupported: true, authorization: .notDetermined, assets: .installed), SG_SPEECH_OK),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .installed), SG_SPEECH_OK),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .supported), SG_SPEECH_ASSET_NOT_INSTALLED),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .downloading), SG_SPEECH_ASSET_NOT_INSTALLED),
            (StatusFacts(localeSupported: true, authorization: .denied, assets: .installed), SG_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: true, authorization: .restricted, assets: .supported), SG_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: true, authorization: .unknown, assets: .installed), SG_SPEECH_NOT_AUTHORIZED),
            (StatusFacts(localeSupported: false, authorization: .denied, assets: .unsupported), SG_SPEECH_LOCALE_UNSUPPORTED),
            (StatusFacts(localeSupported: true, authorization: .authorized, assets: .unsupported), SG_SPEECH_LOCALE_UNSUPPORTED),
        ]
        for (facts, want) in cases {
            StatusProbes.current.withLock { $0 = FakeStatusProbe(facts) }
            XCTAssertEqual(sg_speech_status("en-US"), want, "\(facts)")
            XCTAssertEqual(StatusMapping.code(facts), want)
        }
        XCTAssertEqual(sg_speech_status(nil), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(sg_speech_status("not a locale"), SG_SPEECH_ERR_ARGUMENT)
    }

    func testStatusTimesOutInsteadOfBlockingForever() {
        StatusProbes.current.withLock {
            $0 = FakeStatusProbe(StatusFacts(localeSupported: true, authorization: .authorized, assets: .installed), delaySeconds: 2)
        }
        statusTimeout.withLock { $0 = 0.2 }
        let t0 = Date()
        XCTAssertEqual(sg_speech_status("en-US"), SG_SPEECH_ERR_TIMEOUT)
        XCTAssertLessThan(Date().timeIntervalSince(t0), 1.5)
    }

    /// A compile-time check: each Swift entry point is assigned to a variable whose type is taken
    /// from the header's declaration (not restated), so this file stops compiling when a
    /// prototype in include/snow_globe_speech.h and its @_cdecl implementation in CABI.swift differ
    /// in the number, order or types of parameters or in the result. Swift imports the C
    /// declaration as a Swift function type, so the one difference it lets through is
    /// nullability (an Optional pointer where the header says non-null), which does not change
    /// the C ABI. The library's own build does not catch drift (the @_cdecl functions are not
    /// checked against the header). The calls at the end run the bound functions on argument
    /// checks that need no session.
    func testEntryPointsHaveTheHeadersExactTypes() {
        var status = sg_speech_status
        status = sgSpeechStatusEntry
        var start = sg_speech_start
        start = sgSpeechStartEntry
        var push = sg_speech_push_f32
        push = sgSpeechPushF32Entry
        var finish = sg_speech_finish
        finish = sgSpeechFinishEntry
        var cancel = sg_speech_cancel
        cancel = sgSpeechCancelEntry
        var release = sg_speech_free
        release = sgSpeechFreeEntry
        var contextual = sg_speech_set_contextual_strings
        contextual = sgSpeechSetContextualStringsEntry
        var pad = sg_speech_set_silence_pad_ms
        pad = sgSpeechSetSilencePadMsEntry
        var retention = sg_speech_set_model_retention
        retention = sgSpeechSetModelRetentionEntry
        XCTAssertEqual(status(nil), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertNil(start(nil, 48_000, nil, nil))
        XCTAssertEqual(push(nil, nil, 1), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(finish(nil), SG_SPEECH_ERR_ARGUMENT)
        cancel(nil)
        release(nil)
        XCTAssertEqual(contextual(nil, 0), SG_SPEECH_OK)
        XCTAssertEqual(pad(-1), SG_SPEECH_ERR_ARGUMENT)
        XCTAssertEqual(retention(99), SG_SPEECH_ERR_ARGUMENT)
        print("entry point type: \(type(of: status))")
    }

    func testHeaderCodesAreTheDocumentedValues() {
        XCTAssertEqual(SG_EV_READY.rawValue, 0)
        XCTAssertEqual(SG_EV_PARTIAL.rawValue, 1)
        XCTAssertEqual(SG_EV_FINAL.rawValue, 2)
        XCTAssertEqual(SG_EV_ERROR.rawValue, 3)
        XCTAssertEqual(SG_EV_STABLE.rawValue, 4)
        XCTAssertEqual(MemoryLayout<sg_event_kind>.size, 4)
        XCTAssertEqual(
            [SG_SPEECH_OK, SG_SPEECH_ASSET_NOT_INSTALLED, SG_SPEECH_NOT_AUTHORIZED, SG_SPEECH_LOCALE_UNSUPPORTED],
            [0, 1, 2, 3])
        XCTAssertEqual(
            [SG_SPEECH_ERR_ARGUMENT, SG_SPEECH_ERR_STATE, SG_SPEECH_ERR_OVERFLOW, SG_SPEECH_ERR_AUDIO_FORMAT,
             SG_SPEECH_ERR_ENGINE, SG_SPEECH_ERR_TIMEOUT, SG_SPEECH_ERR_RESOURCES, SG_SPEECH_ERR_ASSET_RESERVATION,
             SG_SPEECH_ERR_INTERNAL],
            [-1, -2, -3, -4, -5, -6, -7, -8, -9])
        XCTAssertEqual(
            [SG_SPEECH_RETAIN_WHILE_IN_USE, SG_SPEECH_RETAIN_LINGERING, SG_SPEECH_RETAIN_PROCESS_LIFETIME], [0, 1, 2])
        XCTAssertEqual(ModelRetention.allCases.map(\.rawValue), [0, 1, 2])
    }
}
