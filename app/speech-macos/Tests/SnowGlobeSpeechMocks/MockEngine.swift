@preconcurrency import AVFoundation
import Foundation
import SnowGlobeSpeech
import SnowGlobeSpeechC
import Synchronization

/// A scripted stand-in for SpeechAnalyzer with DictationTranscriber, put behind the engine seam
/// (`EngineFactory`) by the tests. It never touches the Speech framework, so the tests need no
/// permission, no model and no audio fixture (CLAUDE.md: never record audio).
///
/// It "hears" words by audio time, not by content: after every `secondsPerWord` of audio fed it
/// adds the next word of `words` to a volatile result, and every `finalizeEverySeconds` it
/// finalizes the segment so far, the way a progressive transcriber does. `finish` finalizes the
/// rest. It records what it was fed, so the tests can check the conversion, the silence pad and
/// the threading rules.
package final class MockEngine: RecognizerEngine, @unchecked Sendable {
    package struct Script: Sendable {
        /// The format `prepare` asks for (mono).
        package var sampleRate: Double = 16_000
        package var commonFormat: AVAudioCommonFormat = .pcmFormatInt16
        package var prepareDelaySeconds: Double = 0
        package var prepareError: EngineError?
        package var startError: EngineError?
        package var words: [String] = ["show", "me", "mount", "rainier", "at", "sunset", "tomorrow"]
        package var secondsPerWord: Double = 0.2
        package var finalizeEverySeconds: Double? = 0.6
        /// Reports a failure through `onFailure` once this much audio has been fed.
        package var failAfterSeconds: Double?
        package var finishDelaySeconds: Double = 0
        /// `finish` never returns until `cancel` (to test the session's watchdog).
        package var finishHangs = false
        package var finishError: EngineError?
        /// Deliver results synchronously inside `feed` (true) or later from a queue of its own.
        package var resultsOnFeedThread = true
        /// Sleep this long inside every `feed` (a slow recognizer; for the cancel-latency tests).
        package var feedDelaySeconds: Double = 0
        /// When set, replaces the word logic: each result goes out once `at` seconds of audio have
        /// been fed, and `finish` sends the ones not sent yet, in order.
        package var timeline: [TimedResult]?

        package init() {}
    }

    /// One scripted result and the audio time at which it is reported.
    package struct TimedResult: Sendable {
        package let at: Double
        package let result: RecognizerResult

        package init(at: Double, _ result: RecognizerResult) {
            self.at = at
            self.result = result
        }
    }

    package struct Stats: Sendable {
        package var prepareCalls = 0
        package var startCalls = 0
        package var finishCalls = 0
        package var cancelCalls = 0
        package var buffersFed = 0
        package var framesFed = 0
        /// Every sample fed, as Float (Int16 scaled by 1/32768).
        package var samples: [Float] = []
        /// Feeds that broke the protocol: before start, after finish or cancel, or concurrent.
        package var violations: [String] = []
        /// Feeds that started after the test raised `fence` (it does so when sg_speech_cancel has
        /// returned).
        package var feedsAfterFence = 0
        package var config: EngineConfig?
        package var formatSeen: String = ""
    }

    package let script: Script
    /// Raised by a test once sg_speech_cancel has returned: any feed after it is counted.
    package let fence = Atomic<Bool>(false)
    private let state = Mutex(State())
    private let resultsQueue = DispatchQueue(label: "mock.results")
    private let inFeed = Atomic<Int>(0)

    private struct State {
        var stats = Stats()
        var onResult: (@Sendable (RecognizerResult) -> Void)?
        var onFailure: (@Sendable (EngineError) -> Void)?
        var started = false
        var finished = false
        var cancelled = false
        var failed = false
        var audioSeconds = 0.0
        var segmentStart = 0.0
        var segmentFirstWord = 0
        var lastVolatileWords = 0
        var timelineSent = 0
    }

    package init(_ script: Script = Script()) {
        self.script = script
    }

    package var stats: Stats { state.withLock { $0.stats } }

    package func prepare(_ config: EngineConfig) async throws -> AVAudioFormat {
        state.withLock {
            $0.stats.prepareCalls += 1
            $0.stats.config = config
        }
        if script.prepareDelaySeconds > 0 {
            try? await Task.sleep(for: .seconds(script.prepareDelaySeconds))
        }
        if let e = script.prepareError { throw e }
        guard
            let f = AVAudioFormat(
                commonFormat: script.commonFormat, sampleRate: script.sampleRate, channels: 1, interleaved: false)
        else { throw EngineError(SG_SPEECH_ERR_AUDIO_FORMAT, "mock: bad format") }
        state.withLock { $0.stats.formatSeen = PCMConverter.describe(f) }
        return f
    }

    package func start(
        onResult: @escaping @Sendable (RecognizerResult) -> Void,
        onFailure: @escaping @Sendable (EngineError) -> Void
    ) async throws {
        state.withLock {
            $0.stats.startCalls += 1
            $0.onResult = onResult
            $0.onFailure = onFailure
            $0.started = true
        }
        if let e = script.startError { throw e }
    }

    package func feed(_ buffer: AVAudioPCMBuffer) {
        if fence.load(ordering: .acquiring) {
            state.withLock { $0.stats.feedsAfterFence += 1 }
        }
        if inFeed.wrappingAdd(1, ordering: .acquiringAndReleasing).newValue > 1 {
            state.withLock { $0.stats.violations.append("concurrent feed") }
        }
        defer { _ = inFeed.wrappingSubtract(1, ordering: .acquiringAndReleasing) }
        if script.feedDelaySeconds > 0 { usleep(UInt32(script.feedDelaySeconds * 1e6)) }
        let n = Int(buffer.frameLength)
        var samples = [Float](repeating: 0, count: n)
        if let i16 = buffer.int16ChannelData?[0] {
            for k in 0..<n { samples[k] = Float(i16[k]) / 32_768 }
        } else if let f32 = buffer.floatChannelData?[0] {
            for k in 0..<n { samples[k] = f32[k] }
        }
        let rate = buffer.format.sampleRate
        var out: [RecognizerResult] = []
        var failure: EngineError?
        let (onResult, onFailure) = state.withLock { s -> ((@Sendable (RecognizerResult) -> Void)?, (@Sendable (EngineError) -> Void)?) in
            if !s.started { s.stats.violations.append("feed before start") }
            if s.finished { s.stats.violations.append("feed after finish") }
            if s.cancelled { s.stats.violations.append("feed after cancel") }
            s.stats.buffersFed += 1
            s.stats.framesFed += n
            s.stats.samples.append(contentsOf: samples)
            s.audioSeconds += Double(n) / rate
            out = advance(&s)
            if let limit = script.failAfterSeconds, s.audioSeconds >= limit, !s.failed {
                s.failed = true
                failure = EngineError(SG_SPEECH_ERR_ENGINE, "mock: scripted failure")
            }
            return (s.onResult, s.onFailure)
        }
        deliver(out, onResult)
        if let failure, let onFailure { onFailure(failure) }
    }

    /// The results the audio heard so far calls for (called with the state locked).
    private func advance(_ s: inout State) -> [RecognizerResult] {
        if let timeline = script.timeline {
            var due: [RecognizerResult] = []
            while s.timelineSent < timeline.count, timeline[s.timelineSent].at <= s.audioSeconds {
                due.append(timeline[s.timelineSent].result)
                s.timelineSent += 1
            }
            return due
        }
        var results: [RecognizerResult] = []
        let heard = min(script.words.count, Int(s.audioSeconds / script.secondsPerWord))
        if let every = script.finalizeEverySeconds, s.audioSeconds >= s.segmentStart + every {
            let boundary = s.segmentStart + every
            if heard > s.segmentFirstWord {
                let text = script.words[s.segmentFirstWord..<heard].joined(separator: " ")
                results.append(RecognizerResult(
                    text: text, start: s.segmentStart, end: boundary, finalizedThrough: boundary, isFinal: true))
            }
            s.segmentStart = boundary
            s.segmentFirstWord = heard
            s.lastVolatileWords = heard
        } else if heard > s.lastVolatileWords {
            let text = script.words[s.segmentFirstWord..<heard].joined(separator: " ")
            results.append(RecognizerResult(
                text: text, start: s.segmentStart, end: s.audioSeconds, finalizedThrough: s.segmentStart,
                isFinal: false))
            s.lastVolatileWords = heard
        }
        return results
    }

    private func deliver(_ results: [RecognizerResult], _ onResult: (@Sendable (RecognizerResult) -> Void)?) {
        guard let onResult, !results.isEmpty else { return }
        if script.resultsOnFeedThread {
            results.forEach(onResult)
        } else {
            resultsQueue.async { results.forEach(onResult) }
        }
    }

    package func finish() async throws {
        state.withLock { $0.stats.finishCalls += 1 }
        if script.finishHangs {
            while !state.withLock({ $0.cancelled }) {
                try? await Task.sleep(for: .milliseconds(5))
            }
            throw CancellationError()
        }
        if script.finishDelaySeconds > 0 {
            try? await Task.sleep(for: .seconds(script.finishDelaySeconds))
        }
        if let e = script.finishError { throw e }
        if let timeline = script.timeline {
            let (rest, onResult) = state.withLock { s -> ([RecognizerResult], (@Sendable (RecognizerResult) -> Void)?) in
                s.finished = true
                let rest = timeline[s.timelineSent...].map(\.result)
                s.timelineSent = timeline.count
                return (rest, s.onResult)
            }
            resultsQueue.sync {}
            if let onResult { rest.forEach(onResult) }
            return
        }
        let (last, onResult) = state.withLock { s -> (RecognizerResult?, (@Sendable (RecognizerResult) -> Void)?) in
            s.finished = true
            let heard = min(script.words.count, Int(s.audioSeconds / script.secondsPerWord))
            guard heard > s.segmentFirstWord else { return (nil, s.onResult) }
            let text = script.words[s.segmentFirstWord..<heard].joined(separator: " ")
            s.segmentFirstWord = heard
            return (
                RecognizerResult(
                    text: text, start: s.segmentStart, end: s.audioSeconds, finalizedThrough: s.audioSeconds,
                    isFinal: true),
                s.onResult
            )
        }
        // Results queued on the mock's own queue go out before finish returns, as the protocol says.
        resultsQueue.sync {}
        if let last, let onResult { onResult(last) }
    }

    package func cancel() async {
        state.withLock {
            $0.stats.cancelCalls += 1
            $0.cancelled = true
            $0.onResult = nil
            $0.onFailure = nil
        }
    }
}

/// The engine slot's state for every test: an engine that fails at once and counts its uses.
/// Tests install it in setUp and tearDown, so a test that forgets to install a mock gets an
/// ERROR event, never the real recognizer (which could show a permission prompt).
package final class TrapEngine: RecognizerEngine, @unchecked Sendable {
    package static let uses = Atomic<Int>(0)

    package init() {}

    package func prepare(_ config: EngineConfig) async throws -> AVAudioFormat {
        _ = Self.uses.wrappingAdd(1, ordering: .relaxed)
        throw EngineError(SG_SPEECH_ERR_INTERNAL, "test used the engine slot without installing a mock")
    }

    package func start(
        onResult: @escaping @Sendable (RecognizerResult) -> Void,
        onFailure: @escaping @Sendable (EngineError) -> Void
    ) async throws {
        throw EngineError(SG_SPEECH_ERR_INTERNAL, "trap")
    }

    package func feed(_ buffer: AVAudioPCMBuffer) {}
    package func finish() async throws { throw EngineError(SG_SPEECH_ERR_INTERNAL, "trap") }
    package func cancel() async {}
}

/// A status probe that answers from fixed facts, after an optional delay.
package struct FakeStatusProbe: StatusProbe {
    package let answer: StatusFacts
    package let delaySeconds: Double

    package init(_ answer: StatusFacts, delaySeconds: Double = 0) {
        self.answer = answer
        self.delaySeconds = delaySeconds
    }

    package func facts(locale: String) async -> StatusFacts {
        if delaySeconds > 0 { try? await Task.sleep(for: .seconds(delaySeconds)) }
        return answer
    }
}

/// Keeps every MockEngine a factory made, so a test can read their stats.
package final class MockBox: @unchecked Sendable {
    private let made = Mutex<[MockEngine]>([])

    package init() {}

    package func add(_ m: MockEngine) { made.withLock { $0.append(m) } }
    package var engines: [MockEngine] { made.withLock { $0 } }
    package var last: MockEngine? { engines.last }
}

/// Installs the trap engine and the fake status probe for the whole test process, once, when the
/// test bundle loads: Tests/SnowGlobeSpeechTestBootstrap calls this from a C constructor, before
/// any test runs. So no test, in any class and in any filtered run, can reach the real
/// recognizer or the real status queries (which could prompt) through the process defaults.
@_cdecl("sg_speech_tests_install_trap")
public func sgSpeechTestsInstallTrap() {
    MockInstall.reset()
    MockInstall.installedAtLoad.store(true, ordering: .releasing)
}

/// Puts the package's process-wide seams into test states.
package enum MockInstall {
    /// Set by `sgSpeechTestsInstallTrap` when the test bundle loads.
    package static let installedAtLoad = Atomic<Bool>(false)

    /// The trap engine, a fake "ready" status probe, default settings and the 10 s status timeout.
    package static func reset() {
        EngineFactory.current.withLock { $0 = { TrapEngine() } }
        StatusProbes.current.withLock {
            $0 = FakeStatusProbe(StatusFacts(localeSupported: true, authorization: .notDetermined, assets: .installed))
        }
        Settings.current.withLock { $0 = SessionSettings() }
        statusTimeout.withLock { $0 = 10 }
    }

    /// Every new session gets a MockEngine running `script`; returns the box that keeps them.
    @discardableResult
    package static func mock(_ script: MockEngine.Script = MockEngine.Script()) -> MockBox {
        let box = MockBox()
        EngineFactory.current.withLock { maker in
            maker = {
                let m = MockEngine(script)
                box.add(m)
                return m
            }
        }
        return box
    }
}
