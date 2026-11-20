import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import XCTest

/// The trap is in place for the whole process, not only in SpeechTestCase subclasses: this class
/// derives from XCTestCase directly and installs nothing (README, "What the tests need").
final class ProcessTrapTests: XCTestCase {
    func testTheTrapIsInstalledWhenTheTestBundleLoads() throws {
        XCTAssertTrue(MockInstall.installedAtLoad.load(ordering: .acquiring), "Tests/LookoutSpeechTestBootstrap's initializer ran")
        XCTAssertTrue(EngineFactory.make() is TrapEngine, "the process default is the trap, not DictationEngine")
        // A session started here gets the trap's ERROR, and lk_speech_status gets the fake probe.
        let before = TrapEngine.uses.load(ordering: .acquiring)
        let rec = Recorder()
        let s = try XCTUnwrap(startSession(rec))
        XCTAssertTrue(rec.waitTerminal())
        lk_speech_free(s)
        XCTAssertEqual(rec.events.last?.code, LK_SPEECH_ERR_INTERNAL)
        XCTAssertEqual(TrapEngine.uses.load(ordering: .acquiring), before + 1)
        XCTAssertEqual(lk_speech_status("en-US"), LK_SPEECH_OK, "the fake probe answers")
    }
}
