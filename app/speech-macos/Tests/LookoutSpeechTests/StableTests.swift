import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import XCTest

/// LK_EV_STABLE: the stable part of the hypothesis (PRD §7.1 Partial "text, stability";
/// lever 2, "Tier 0 painted from a stable partial"; §10.1 "or stable partial").
final class StableTests: SpeechTestCase {
    private func r(_ t: String, _ a: Double, _ b: Double, final: Bool, through: Double) -> RecognizerResult {
        RecognizerResult(text: t, start: a, end: b, finalizedThrough: through, isFinal: final)
    }

    private func isBytePrefix(_ p: String, of s: String) -> Bool { s.utf8.starts(with: p.utf8) }

    /// Runs one session to its terminal event and returns what the recorder saw. `beforeFinish`
    /// runs while the key is still "held" (pushAll returns before the drain has fed the audio, so
    /// a test that reasons about events before key-up waits for them there).
    private func run(
        _ script: MockEngine.Script, seconds: Double, beforeFinish: (Recorder) -> Void = { _ in }
    ) throws -> [SeenEvent] {
        MockInstall.mock(script)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        pushAll(s, tone(seconds: seconds, rate: 48_000))
        beforeFinish(rec)
        rec.finishCalled.store(true, ordering: .releasing)
        XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
        XCTAssertTrue(rec.waitTerminal())
        lk_speech_free(s)
        return rec.events
    }

    /// Every STABLE is a byte prefix of the latest PARTIAL before it and of the FINAL, STABLEs
    /// only grow, and nothing follows the FINAL.
    private func assertStableInvariants(_ ev: [SeenEvent], file: StaticString = #filePath, line: UInt = #line) {
        var latestPartial: String?
        var previousStable = ""
        for e in ev {
            switch e.kind {
            case LK_EV_PARTIAL.rawValue:
                latestPartial = e.text
            case LK_EV_STABLE.rawValue:
                guard let p = latestPartial else {
                    XCTFail("a STABLE came before any PARTIAL", file: file, line: line)
                    continue
                }
                XCTAssertTrue(isBytePrefix(e.text, of: p), "STABLE '\(e.text)' is not the start of '\(p)'", file: file, line: line)
                XCTAssertTrue(isBytePrefix(previousStable, of: e.text), "STABLE shrank: '\(previousStable)' -> '\(e.text)'", file: file, line: line)
                XCTAssertEqual(e.code, 0, file: file, line: line)
                XCTAssertGreaterThanOrEqual(e.end, e.start, file: file, line: line)
                previousStable = e.text
            default:
                break
            }
        }
        guard let final = ev.last, final.kind == LK_EV_FINAL.rawValue else {
            return XCTFail("the session did not end in FINAL", file: file, line: line)
        }
        XCTAssertTrue(isBytePrefix(previousStable, of: final.text), "FINAL '\(final.text)' does not start with '\(previousStable)'", file: file, line: line)
    }

    func testStableTracksTheFinalizedTextAndIsAlwaysThePrefixOfTheLatestPartial() throws {
        var script = MockEngine.Script()
        script.finalizeEverySeconds = 0.4
        // 1.3 s: the mock finalizes at 0.4, 0.8 and 1.2 s of audio, all before key-up.
        let ev = try run(script, seconds: 1.3) { rec in
            XCTAssertTrue(rec.waitForCount(LK_EV_STABLE, 3), "three STABLEs while the key is held")
        }
        assertStableInvariants(ev)
        let stables = ev.filter { $0.kind == LK_EV_STABLE.rawValue }
        XCTAssertEqual(stables.map(\.text), ["show me", "show me mount rainier", "show me mount rainier at sunset"])
        XCTAssertEqual(stables.map(\.start), [0, 0, 0])
        for (s, end) in zip(stables, [0.4, 0.8, 1.2]) { XCTAssertEqual(s.end, end, accuracy: 1e-9) }
        XCTAssertEqual(ev.last?.text, "show me mount rainier at sunset")
        XCTAssertTrue(stables.allSatisfy { !$0.afterFinishCall }, "these were finalized while the key was held")
    }

    func testAResultFinalizedUnchangedSendsAStableAndNoPartial() throws {
        var script = MockEngine.Script()
        script.timeline = [
            .init(at: 0.05, r("show me", 0, 0.3, final: false, through: 0)),
            .init(at: 0.35, r("show me", 0, 0.3, final: true, through: 0.3)),
            .init(at: 0.45, r("mount ray", 0.3, 0.45, final: false, through: 0.3)),
            // Sent at finish: the tentative "ray" is finalized as "rainier".
            .init(at: 99, r("mount rainier", 0.3, 0.6, final: true, through: 0.6)),
        ]
        let ev = try run(script, seconds: 0.6)
        assertStableInvariants(ev)
        let seen = ev.map { (Kind($0.kind), $0.text) }
        XCTAssertEqual(seen.map(\.0), [.ready, .partial, .stable, .partial, .partial, .stable, .final])
        XCTAssertEqual(seen.map(\.1), [
            "", "show me", "show me", "show me mount ray", "show me mount rainier", "show me mount rainier",
            "show me mount rainier",
        ])
        let lastStable = try XCTUnwrap(ev.last { $0.kind == LK_EV_STABLE.rawValue })
        XCTAssertEqual(lastStable.start, 0)
        XCTAssertEqual(lastStable.end, 0.6, accuracy: 1e-9)
        XCTAssertTrue(lastStable.afterFinishCall)
    }

    func testStablePrefixesHoldByteForByteForNonASCIIText() throws {
        var script = MockEngine.Script()
        script.timeline = [
            .init(at: 0.05, r("Yose\u{301}mite", 0, 0.3, final: false, through: 0)),
            .init(at: 0.32, r("Yosémite", 0, 0.3, final: true, through: 0.3)),
            .init(at: 0.40, r("naïve café", 0.3, 0.4, final: false, through: 0.3)),
            .init(at: 99, r("naïve café 🏔️", 0.3, 0.6, final: true, through: 0.6)),
        ]
        let ev = try run(script, seconds: 0.6)
        assertStableInvariants(ev)
        // The precomposed é replaced the decomposed one: Swift's String equality calls them equal,
        // but the bytes differ, so a PARTIAL goes out and the caller's text stays consistent.
        XCTAssertEqual(ev.filter { $0.kind == LK_EV_PARTIAL.rawValue }.map { Array($0.text.utf8) }.first,
                       Array("Yose\u{301}mite".utf8))
        XCTAssertEqual(ev.last?.text, "Yosémite naïve café 🏔️")
    }

    func testNothingIsStableBeforeTheRecognizerFinalizes() throws {
        var script = MockEngine.Script()
        script.finalizeEverySeconds = nil
        let ev = try run(script, seconds: 1.1) { rec in
            // Every word heard (five, as volatile text), and still nothing stable.
            XCTAssertTrue(rec.waitForText(LK_EV_PARTIAL, "show me mount rainier at"))
            Thread.sleep(forTimeInterval: 0.05)
            XCTAssertEqual(rec.count(LK_EV_STABLE), 0)
        }
        assertStableInvariants(ev)
        let stables = ev.filter { $0.kind == LK_EV_STABLE.rawValue }
        XCTAssertEqual(stables.count, 1, "one STABLE, when finish finalizes everything")
        XCTAssertTrue(stables.allSatisfy(\.afterFinishCall))
        XCTAssertEqual(stables.first?.text, ev.last?.text)
        XCTAssertGreaterThan(ev.filter { $0.kind == LK_EV_PARTIAL.rawValue && !$0.afterFinishCall }.count, 0)
    }
}

/// Event kinds by name, for readable sequence assertions.
private enum Kind: Equatable {
    case ready, partial, stable, final, error, other(UInt32)

    init(_ kind: UInt32) {
        switch kind {
        case LK_EV_READY.rawValue: self = .ready
        case LK_EV_PARTIAL.rawValue: self = .partial
        case LK_EV_STABLE.rawValue: self = .stable
        case LK_EV_FINAL.rawValue: self = .final
        case LK_EV_ERROR.rawValue: self = .error
        default: self = .other(kind)
        }
    }
}
