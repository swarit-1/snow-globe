@preconcurrency import AVFoundation
import LookoutSpeechC
import Synchronization

/// A failure with one of the C ABI's codes (include/lookout_speech.h) and a diagnostic message.
/// The message never contains transcript text (PRD §10.5).
package struct EngineError: Error, Equatable, Sendable, CustomStringConvertible {
    package let code: Int32
    package let detail: String

    package init(_ code: Int32, _ detail: String) {
        self.code = code
        self.detail = detail
    }

    package var description: String { "\(code): \(detail)" }
}

/// What a session asks of the recognizer.
package struct EngineConfig: Sendable, Equatable {
    /// BCP 47, as the caller gave it.
    package let locale: String
    /// The session's input rate; the recognizer may use it to pick its own format.
    package let inputSampleRate: Double
    package let contextualStrings: [String]
    package let retention: ModelRetention

    package init(locale: String, inputSampleRate: Double, contextualStrings: [String], retention: ModelRetention) {
        self.locale = locale
        self.inputSampleRate = inputSampleRate
        self.contextualStrings = contextualStrings
        self.retention = retention
    }
}

/// One result from the recognizer, in seconds of audio since the session's first sample.
///
/// It mirrors `SpeechModuleResult`: `isFinal` means no later result will cover this range;
/// otherwise the result is volatile and may be replaced. `finalizedThrough` is the time up to
/// which every result has been finalized (`resultsFinalizationTime`).
package struct RecognizerResult: Sendable, Equatable {
    package let text: String
    package let start: Double
    package let end: Double
    package let finalizedThrough: Double
    package let isFinal: Bool

    package init(text: String, start: Double, end: Double, finalizedThrough: Double, isFinal: Bool) {
        self.text = text
        self.start = start
        self.end = end
        self.finalizedThrough = finalizedThrough
        self.isFinal = isFinal
    }
}

/// The recognizer behind a session (PRD §7.1, the `SpeechSession` seam). The library's only
/// implementation is `DictationEngine`, SpeechAnalyzer with DictationTranscriber; the tests
/// substitute a mock through `EngineFactory`, so the session's buffering, conversion, event
/// rules and cancellation are tested without the real recognizer (which could prompt).
///
/// The session calls `prepare`, then `start`, then any number of `feed`s, then `finish` or
/// `cancel`. `feed` is only called from the session's work queue.
package protocol RecognizerEngine: AnyObject, Sendable {
    /// Checks readiness and sets up the recognizer. Returns the PCM format `feed` must receive.
    /// Throws `EngineError` (codes 1-3 for readiness problems, negative codes otherwise).
    func prepare(_ config: EngineConfig) async throws -> AVAudioFormat

    /// Starts analysis. Results go to `onResult` in order, from one task. A failure after the
    /// start goes to `onFailure` once; neither is called after `cancel`.
    func start(
        onResult: @escaping @Sendable (RecognizerResult) -> Void,
        onFailure: @escaping @Sendable (EngineError) -> Void
    ) async throws

    /// Queues one buffer in the prepared format. Must not block.
    func feed(_ buffer: AVAudioPCMBuffer)

    /// Ends the input, finalizes, and returns once every result has gone to `onResult`.
    func finish() async throws

    /// Stops at once and releases the recognizer and any audio it holds.
    func cancel() async
}

/// Makes the engine for each new session. Package access: only the package's tests replace it
/// (with LookoutSpeechMocks); nothing outside this package can.
package enum EngineFactory {
    package typealias Maker = @Sendable () -> any RecognizerEngine

    package static let real: Maker = { DictationEngine() }
    package static let current = Mutex<Maker>(real)

    package static func make() -> any RecognizerEngine {
        let maker = current.withLock { $0 }
        return maker()
    }
}
