import Darwin
import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import XCTest

/// Sessions are released after `lk_speech_free` (each holds a ring of at least 20 s of audio,
/// 4 MB at 48 kHz; PRD §15.1: audio is held for the query only).
final class LeakTests: SpeechTestCase {
    private func session(_ s: OpaquePointer) -> Session {
        Unmanaged<Session>.fromOpaque(UnsafeRawPointer(s)).takeUnretainedValue()
    }

    private func waitUntilReleased(_ probe: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(3)
        while !probe() {
            if Date() > deadline { return false }
            usleep(2_000)
        }
        return true
    }

    /// The Session object itself goes away, whichever way the session ended.
    func testAFreedSessionIsDeallocatedInEveryEndState() throws {
        enum End: CaseIterable { case final, error, cancelledDuringSetup, freedWithoutFinish, freedInFinalCallback }
        for end in End.allCases {
            var script = MockEngine.Script()
            if end == .error { script.failAfterSeconds = 0.1 }
            if end == .cancelledDuringSetup { script.prepareDelaySeconds = 0.2 }
            MockInstall.mock(script)
            let rec = Recorder()
            let holder = SessionHolder()
            let freedInCallback = DispatchSemaphore(value: 0)
            if end == .freedInFinalCallback {
                rec.hook = { e in
                    if e.kind == LK_EV_FINAL.rawValue, let s = holder.session {
                        lk_speech_free(s)
                        freedInCallback.signal()
                    }
                }
            }
            weak var weakSession: Session?
            do {
                let s = try XCTUnwrap(startSession(rec))
                holder.session = s
                weakSession = session(s)
                pushAll(s, tone(seconds: 0.3, rate: 48_000))
                switch end {
                case .final:
                    XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
                    XCTAssertTrue(rec.waitTerminal())
                    lk_speech_free(s)
                case .error:
                    XCTAssertTrue(rec.waitTerminal())
                    lk_speech_free(s)
                case .cancelledDuringSetup:
                    lk_speech_cancel(s)
                    lk_speech_free(s)
                case .freedWithoutFinish:
                    lk_speech_free(s)
                case .freedInFinalCallback:
                    XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
                    XCTAssertEqual(freedInCallback.wait(timeout: .now() + 5), .success)
                }
            }
            XCTAssertTrue(waitUntilReleased { weakSession == nil }, "\(end): the session was not released")
        }
    }

    /// The process's footprint after many sessions: a leaked ring per session would add 4 MB each
    /// (150 sessions: 600 MB; 500 start+free cycles: 2 GB).
    func testManySessionsDoNotGrowTheFootprint() throws {
        func fullSessions(_ n: Int) throws {
            for _ in 0..<n {
                MockInstall.mock()  // a new box each time, so the mock's recordings are released too
                let rec = Recorder()
                let s = try XCTUnwrap(startSession(rec, rate: 48_000))
                pushAll(s, tone(seconds: 0.3, rate: 48_000))
                XCTAssertEqual(lk_speech_finish(s), LK_SPEECH_OK)
                XCTAssertTrue(rec.waitTerminal())
                lk_speech_free(s)
            }
        }
        try fullSessions(5)  // warm-up: first-use allocations
        Thread.sleep(forTimeInterval: 0.3)
        let before = try XCTUnwrap(physFootprint())
        try fullSessions(150)
        Thread.sleep(forTimeInterval: 0.3)
        let afterSessions = try XCTUnwrap(physFootprint())
        for _ in 0..<500 {
            MockInstall.mock()
            let rec = Recorder()
            lk_speech_free(try XCTUnwrap(startSession(rec, rate: 48_000)))
        }
        Thread.sleep(forTimeInterval: 0.5)
        let afterCycles = try XCTUnwrap(physFootprint())
        let mb = { (b: UInt64) in Double(b) / 1_048_576 }
        print(String(
            format: "footprint (MB): before %.1f, after 150 full sessions %.1f, after 500 start+free cycles %.1f",
            mb(before), mb(afterSessions), mb(afterCycles)))
        XCTAssertLessThan(mb(afterSessions) - mb(before), 32, "150 sessions")
        XCTAssertLessThan(mb(afterCycles) - mb(afterSessions), 32, "500 start+free cycles")
    }
}

/// The process's physical footprint (what Activity Monitor shows as Memory), from proc_pid_rusage.
func physFootprint() -> UInt64? {
    var info = rusage_info_v4()
    let rc = withUnsafeMutablePointer(to: &info) { p in
        p.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0) }
    }
    return rc == 0 ? info.ri_phys_footprint : nil
}
