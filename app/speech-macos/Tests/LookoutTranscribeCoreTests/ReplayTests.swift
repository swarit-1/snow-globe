import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import LookoutTranscribeCore
import XCTest

/// lookout-transcribe's pipeline (WAV -> C ABI -> JSON lines) with the scripted engine behind
/// the seam. The real recognizer is never called here (it could prompt), and no voice is used:
/// the input is a generated tone.
final class ReplayTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockInstall.reset()
    }

    override func tearDown() {
        MockInstall.reset()
        super.tearDown()
    }

    private func audio(seconds: Double, rate: Double = 16_000) throws -> WAVAudio {
        let x = sine(Int(seconds * rate), rate: rate, hz: 440, amplitude: 0.5)
        return try WAVReader.parse(WAVWriter.make([x], rate: UInt32(rate), kind: .pcm16))
    }

    private func objects(_ lines: [String]) throws -> [[String: Any]] {
        try lines.map { try XCTUnwrap(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]) }
    }

    func testFastReplayWritesMetaEventsAndSummary() throws {
        let box = MockInstall.mock()
        var o = ReplayOptions()
        o.realtime = false
        o.sourceName = "tone.wav"
        o.contextualStrings = ["Mount Rainier"]
        o.silencePadMs = 100
        let out = Replay.run(audio: try audio(seconds: 1.5), options: o)
        XCTAssertEqual(out.exitCode, 0)
        let objs = try objects(out.lines)
        XCTAssertEqual(objs.first?["type"] as? String, "meta")
        XCTAssertEqual(objs.last?["type"] as? String, "summary")
        let meta = objs[0]
        XCTAssertEqual(meta["file"] as? String, "tone.wav")
        XCTAssertEqual(meta["pacing"] as? String, "fast")
        XCTAssertEqual(meta["silence_pad_ms"] as? Int, 100)
        XCTAssertEqual(meta["contextual_strings"] as? Int, 1)
        XCTAssertTrue((meta["library"] as? String)?.hasPrefix("lookout-speech ") ?? false)
        XCTAssertEqual(meta["fast_speedup"] as? Double, 20)
        XCTAssertTrue((meta["timings"] as? String)?.hasPrefix("diagnostic:") ?? false, "never presented as the PRD 10.6 measure")
        let events = objs.filter { $0["type"] as? String == "event" }
        XCTAssertEqual(events.first?["kind"] as? String, "ready")
        XCTAssertEqual(events.last?["kind"] as? String, "final")
        XCTAssertEqual(events.last?["text"] as? String, "show me mount rainier at sunset tomorrow")
        XCTAssertEqual(events.last?["words"] as? Int, 7)
        for e in events {
            for k in ["host_ns", "wall_s", "audio_pushed_s", "audio_start_s", "audio_end_s", "error_code", "text_len"] {
                XCTAssertNotNil(e[k], "event has \(k)")
            }
        }
        let summary = try XCTUnwrap(objs.last)
        XCTAssertEqual(summary["terminal"] as? String, "final")
        XCTAssertEqual(summary["finals"] as? Int, 1)
        XCTAssertGreaterThan(summary["stables"] as? Int ?? 0, 0, "the mock finalizes as it goes")
        XCTAssertTrue(events.contains { $0["kind"] as? String == "stable" })
        XCTAssertEqual(summary["finish_rc"] as? Int, 0)
        XCTAssertNotNil(summary["finish_to_terminal_ms"])
        XCTAssertNotNil(summary["first_partial_wall_s"])
        let push = try XCTUnwrap(summary["push"] as? [String: Any])
        XCTAssertEqual(push["count"] as? Int, 150, "1.5 s in 10 ms pushes")
        XCTAssertEqual(push["frames_per_push"] as? Int, 160)
        XCTAssertEqual(box.last?.stats.config?.contextualStrings, ["Mount Rainier"])
    }

    /// --fast on a file longer than the library's ring (25 s at 48 kHz; the ring holds 21.8 s)
    /// while setup takes 1.5 s: it waits for READY after 15 s instead of overflowing (ERROR -3).
    func testFastReplayOfAFileLongerThanTheRingWaitsForReadyInsteadOfOverflowing() throws {
        var script = MockEngine.Script()
        script.prepareDelaySeconds = 1.5
        let box = MockInstall.mock(script)
        var o = ReplayOptions()
        o.realtime = false
        o.includeText = false
        let t0 = Date()
        let out = Replay.run(audio: try audio(seconds: 25, rate: 48_000), options: o)
        let took = Date().timeIntervalSince(t0)
        XCTAssertEqual(out.exitCode, 0)
        let summary = try XCTUnwrap(try objects(out.lines).last)
        XCTAssertEqual(summary["terminal"] as? String, "final")
        let push = try XCTUnwrap(summary["push"] as? [String: Any])
        XCTAssertEqual(push["errors"] as? [String: Int], [:], "no push was refused")
        let waited = try XCTUnwrap(summary["fast_waited_for_ready_ms"] as? Double)
        XCTAssertGreaterThan(waited, 300, "15 s at 20x is 0.75 s, setup took 1.5 s")
        XCTAssertLessThan(took, 10, "still much faster than the 25 s of audio")
        XCTAssertEqual(Double(box.last?.stats.framesFed ?? 0), 25 * 16_000, accuracy: 64, "all of it reached the recognizer")
    }

    func testEventKindNames() {
        XCTAssertEqual(
            [LK_EV_READY, LK_EV_PARTIAL, LK_EV_STABLE, LK_EV_FINAL, LK_EV_ERROR].map { Replay.kindName($0.rawValue) },
            ["ready", "partial", "stable", "final", "error"])
        XCTAssertEqual(Replay.kindName(9), "unknown-9")
    }

    func testNoTextModeKeepsLengthsOnly() throws {
        MockInstall.mock()
        var o = ReplayOptions()
        o.realtime = false
        o.includeText = false
        // 1.1 s: five words, off the mock's 0.2 s word boundary.
        let out = Replay.run(audio: try audio(seconds: 1.1), options: o)
        XCTAssertEqual(out.exitCode, 0)
        let joined = out.lines.joined(separator: "\n")
        for word in ["show", "mount", "rainier"] {
            XCTAssertFalse(joined.contains(word), "transcript text leaked: \(word)")
        }
        let final = try XCTUnwrap(try objects(out.lines).last { $0["kind"] as? String == "final" })
        XCTAssertNil(final["text"])
        XCTAssertEqual(final["words"] as? Int, 5)
    }

    func testRealtimePacingFollowsTheAudioClock() throws {
        MockInstall.mock()
        var o = ReplayOptions()
        o.realtime = true
        let t0 = Date()
        let out = Replay.run(audio: try audio(seconds: 0.5), options: o)
        let took = Date().timeIntervalSince(t0)
        XCTAssertEqual(out.exitCode, 0)
        XCTAssertGreaterThanOrEqual(took, 0.5, "half a second of audio takes half a second")
        let summary = try XCTUnwrap(try objects(out.lines).last)
        let lastPush = try XCTUnwrap(summary["last_push_wall_s"] as? Double)
        XCTAssertEqual(lastPush, 0.5, accuracy: 0.05)
        // At every event, the audio pushed so far tracks the wall clock.
        for e in try objects(out.lines) where e["type"] as? String == "event" && e["kind"] as? String == "partial" {
            let wall = try XCTUnwrap(e["wall_s"] as? Double)
            let pushed = try XCTUnwrap(e["audio_pushed_s"] as? Double)
            XCTAssertEqual(pushed, wall, accuracy: 0.05)
        }
    }

    func testAnErrorEndsWithExitCode1AndKeepsTheDiagnostic() throws {
        var script = MockEngine.Script()
        script.prepareError = EngineError(LK_SPEECH_ASSET_NOT_INSTALLED, "mock: no model")
        MockInstall.mock(script)
        var o = ReplayOptions()
        o.realtime = false
        o.includeText = false
        let out = Replay.run(audio: try audio(seconds: 0.3), options: o)
        XCTAssertEqual(out.exitCode, 1)
        let objs = try objects(out.lines)
        let err = try XCTUnwrap(objs.first { $0["kind"] as? String == "error" })
        XCTAssertEqual(err["error_code"] as? Int, Int(LK_SPEECH_ASSET_NOT_INSTALLED))
        XCTAssertEqual(err["text"] as? String, "mock: no model", "an ERROR's text is a diagnostic and is kept")
        XCTAssertEqual(objs.last?["terminal"] as? String, "error")
    }

    func testARejectedSettingStopsBeforeAnySession() throws {
        let box = MockInstall.mock()
        var o = ReplayOptions()
        o.contextualStrings = [" "]
        let out = Replay.run(audio: try audio(seconds: 0.1), options: o)
        XCTAssertEqual(out.exitCode, 2)
        XCTAssertTrue(box.engines.isEmpty)
    }

    func testStatusText() {
        XCTAssertEqual(StatusText.meaning(0), "ready")
        XCTAssertEqual(StatusText.meaning(1), "model asset not installed")
        XCTAssertEqual(StatusText.meaning(2), "not authorized")
        XCTAssertEqual(StatusText.meaning(3), "locale unsupported")
        XCTAssertEqual(StatusText.meaning(-6), "timeout")
        XCTAssertEqual(StatusText.meaning(42), "undocumented code")
        XCTAssertEqual(
            StatusText.statusLine(locale: "en-US", code: 1),
            #"{"code":1,"locale":"en-US","meaning":"model asset not installed","type":"status"}"#)
    }
}

final class ArgumentsTests: XCTestCase {
    func testTranscribeOptions() throws {
        let c = try Arguments.parse([
            "--locale", "en-GB", "--chunk-ms", "20", "--fast", "--no-text", "--context", "Point Reyes",
            "--context", "Yosemite", "--pad-ms", "250", "--retention", "process-lifetime", "--timeout-s", "5",
            "/some/where/fixture 01.wav",
        ])
        guard case .transcribe(let path, let o) = c else { return XCTFail("\(c)") }
        XCTAssertEqual(path, "/some/where/fixture 01.wav")
        XCTAssertEqual(o.sourceName, "fixture 01.wav", "the meta line names the file, never its folder")
        XCTAssertEqual(o.locale, "en-GB")
        XCTAssertEqual(o.chunkMs, 20)
        XCTAssertFalse(o.realtime)
        XCTAssertFalse(o.includeText)
        XCTAssertEqual(o.contextualStrings, ["Point Reyes", "Yosemite"])
        XCTAssertEqual(o.silencePadMs, 250)
        XCTAssertEqual(o.retention, LK_SPEECH_RETAIN_PROCESS_LIFETIME)
        XCTAssertEqual(o.terminalTimeoutSeconds, 5)
    }

    func testDefaultsAndSubcommands() throws {
        guard case .transcribe(_, let o) = try Arguments.parse(["a.wav"]) else { return XCTFail() }
        XCTAssertEqual(o, { var d = ReplayOptions(); d.sourceName = "a.wav"; return d }())
        XCTAssertTrue(o.realtime)
        XCTAssertTrue(o.includeText)
        XCTAssertNil(o.silencePadMs)
        XCTAssertEqual(try Arguments.parse([]), .help)
        XCTAssertEqual(try Arguments.parse(["--help"]), .help)
        XCTAssertEqual(try Arguments.parse(["version"]), .version)
        XCTAssertEqual(try Arguments.parse(["status"]), .status(locale: "en-US"))
        XCTAssertEqual(try Arguments.parse(["status", "--locale", "fr-FR"]), .status(locale: "fr-FR"))
        XCTAssertEqual(try Arguments.parse(["install-assets", "--locale", "en-US"]), .installAssets(locale: "en-US", confirmed: false))
        XCTAssertEqual(try Arguments.parse(["install-assets", "--yes", "--locale", "en-US"]), .installAssets(locale: "en-US", confirmed: true))
    }

    func testBadArgumentsAreUsageErrors() {
        for bad in [["--chunk-ms", "0"], ["--pad-ms", "5000", "a.wav"], ["--retention", "forever", "a.wav"],
                    ["a.wav", "b.wav"], ["--bogus", "a.wav"], ["--locale"], ["--fast"], ["install-assets"],
                    ["status", "--yes"], ["--timeout-s", "-1", "a.wav"]] {
            XCTAssertThrowsError(try Arguments.parse(bad), "\(bad)") { XCTAssertTrue($0 is UsageError) }
        }
    }
}
