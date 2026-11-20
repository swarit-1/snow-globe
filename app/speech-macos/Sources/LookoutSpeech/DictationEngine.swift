@preconcurrency import AVFoundation
import CoreMedia
import Foundation
import LookoutSpeechC
import Speech
import Synchronization

/// The transcriber configuration every session and `lk_speech_status` use (PRD §7.1 engine A1,
/// D-015). One place, so the status check asks about the same model the sessions load.
///
/// `progressiveShortDictation` is Apple's preset for "immediate transcription of about a minute of
/// live audio" (SDK 26.2 documentation), which fits a held key. `volatileResults` ("tentative
/// results for an audio range in addition to the finalized result") gives the partials; it is
/// added in case the preset ever drops it. Everything else is the preset's own.
package enum TranscriberSetup {
    package static let presetName = "progressiveShortDictation"

    package static func make(locale: Locale) -> DictationTranscriber {
        let p = DictationTranscriber.Preset.progressiveShortDictation
        return DictationTranscriber(
            locale: locale,
            contentHints: p.contentHints,
            transcriptionOptions: p.transcriptionOptions,
            reportingOptions: p.reportingOptions.union([.volatileResults]),
            attributeOptions: p.attributeOptions)
    }
}

/// The app's Speech Recognition authorization, read without asking the user
/// (`SFSpeechRecognizer.authorizationStatus()`; `requestAuthorization` is the call that prompts,
/// and this library never makes it).
///
/// Apple's "Asking Permission to Use Speech Recognition" says the authorization "only applies to
/// speech recognition using SFSpeechRecognizer. SpeechAnalyzer transcriber modules don't send
/// audio data of the user's voice to Apple's servers." So "not determined" does not stop a
/// session. "Denied" and "restricted" do: the user or a profile has said no to speech recognition
/// for this app, and the library honours that even though the on-device module would not need it.
package enum SpeechAuthorization: String, Sendable, Equatable {
    case notDetermined = "not-determined"
    case denied
    case restricted
    case authorized
    case unknown

    package static func current() -> SpeechAuthorization {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .notDetermined: .notDetermined
        case .denied: .denied
        case .restricted: .restricted
        case .authorized: .authorized
        @unknown default: .unknown
        }
    }

    /// True when a session must not start. An unknown future status also blocks (privacy first).
    package var blocks: Bool {
        switch self {
        case .denied, .restricted, .unknown: true
        case .notDetermined, .authorized: false
        }
    }
}

/// The model asset state for a module configuration (`AssetInventory.Status`).
package enum AssetState: String, Sendable, Equatable {
    case unsupported, supported, downloading, installed

    init(_ s: AssetInventory.Status) {
        switch s {
        case .unsupported: self = .unsupported
        case .supported: self = .supported
        case .downloading: self = .downloading
        case .installed: self = .installed
        @unknown default: self = .unsupported
        }
    }
}

/// Retries a setup step once after reserving the locale's model for this app.
package enum AssetReservation {
    /// Runs `body`. If it fails with `isUnreserved` (SpeechAnalyzer's `assetLocaleNotAllocated`:
    /// "The asset locale has not been allocated, but module requires it"), calls `reserve` and runs
    /// `body` once more. A reservation only records that this app uses a model that is already
    /// installed (`AssetInventory.reserve(locale:)`); it downloads nothing. Reservations belong to
    /// the app's identity, so a model installed through lookout-transcribe may still need one for
    /// the M1 spike or the shell. TODO(M1): which identity that is (the embedded Info.plist's
    /// bundle identifier, or the code-signing identifier, which for an ad-hoc cargo build changes
    /// with every build hash) is unverified; if it is the signing identifier, repeated reservations
    /// could reach `AssetInventory.maximumReservedLocales` (then ERROR -8). README, "What needs the
    /// owner".
    package static func retryingOnce<T>(
        isUnreserved: (any Error) -> Bool,
        reserve: () async throws -> Void,
        _ body: () async throws -> T
    ) async throws -> T {
        do {
            return try await body()
        } catch where isUnreserved(error) {
            do {
                try await reserve()
            } catch {
                throw EngineError(
                    LK_SPEECH_ERR_ASSET_RESERVATION,
                    "the model is installed but could not be reserved for this app: \(ErrorMapping.describe(error))")
            }
            return try await body()
        }
    }
}

/// SpeechAnalyzer with DictationTranscriber: the library's only recognizer (PRD §7.1 A1).
///
/// On device only. DictationTranscriber "uses the same speech-to-text machine learning models as
/// system dictation features do, or as SFSpeechRecognizer does when it is configured for
/// on-device operation", and "does not support languages or locales that SFSpeechRecognizer only
/// supports via network access" (SDK 26.2 documentation). This engine never uses
/// SFSpeechRecognizer's recognition tasks (which can use Apple's servers) and never downloads a
/// model: a missing model is LK_SPEECH_ASSET_NOT_INSTALLED, not a fallback. The M1 spike's check 7
/// confirms the network silence with a monitor (PRD §7.1, D-015).
///
/// The analyzer runs autonomously (`start(inputSequence:)`) on an `AsyncStream` of converted
/// buffers that the session feeds from its work queue. At key-up the stream is finished and
/// `finalizeAndFinishThroughEndOfInput()` finalizes everything (Apple's live-audio pattern).
///
/// Never run by the tests: calling the real recognizer could show a permission prompt.
final class DictationEngine: RecognizerEngine, @unchecked Sendable {
    private struct Parts {
        var analyzer: SpeechAnalyzer?
        var transcriber: DictationTranscriber?
        var input: AsyncStream<AnalyzerInput>.Continuation?
        var results: Task<Void, Never>?
        var cancelled = false
    }

    private let parts = Mutex(Parts())

    private var isCancelled: Bool { parts.withLock { $0.cancelled } }

    func prepare(_ config: EngineConfig) async throws -> AVAudioFormat {
        let requested = Locale(identifier: config.locale)
        guard let locale = await DictationTranscriber.supportedLocale(equivalentTo: requested) else {
            throw EngineError(
                LK_SPEECH_LOCALE_UNSUPPORTED, "DictationTranscriber has no on-device model for \(config.locale)")
        }
        let auth = SpeechAuthorization.current()
        if auth.blocks {
            throw EngineError(LK_SPEECH_NOT_AUTHORIZED, "Speech Recognition is \(auth.rawValue) for this app")
        }
        switch AssetState(await AssetInventory.status(forModules: [TranscriberSetup.make(locale: locale)])) {
        case .installed:
            break
        case .unsupported:
            throw EngineError(LK_SPEECH_LOCALE_UNSUPPORTED, "no on-device dictation model exists for \(config.locale)")
        case .supported, .downloading:
            throw EngineError(
                LK_SPEECH_ASSET_NOT_INSTALLED,
                "the on-device dictation model for \(config.locale) is not installed; this library never downloads it (see app/speech-macos/README.md)")
        }
        let natural = AVAudioFormat(standardFormatWithSampleRate: config.inputSampleRate, channels: 1)
        let built = try await AssetReservation.retryingOnce(
            isUnreserved: ErrorMapping.isUnreservedLocale,
            reserve: { _ = try await AssetInventory.reserve(locale: locale) },
            { try await Self.build(locale: locale, natural: natural, config: config) })
        let cancelled = parts.withLock { p -> Bool in
            if p.cancelled { return true }
            p.analyzer = built.analyzer
            p.transcriber = built.transcriber
            return false
        }
        if cancelled {
            await built.analyzer.cancelAndFinishNow()
            throw EngineError(LK_SPEECH_ERR_STATE, "cancelled during setup")
        }
        return built.format
    }

    private struct Built: @unchecked Sendable {
        let analyzer: SpeechAnalyzer
        let transcriber: DictationTranscriber
        let format: AVAudioFormat
    }

    private static func build(locale: Locale, natural: AVAudioFormat?, config: EngineConfig) async throws -> Built {
        let transcriber = TranscriberSetup.make(locale: locale)
        guard
            let format = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [transcriber], considering: natural)
        else {
            throw EngineError(LK_SPEECH_ERR_AUDIO_FORMAT, "SpeechAnalyzer offered no audio format for this module")
        }
        let analyzer = SpeechAnalyzer(
            modules: [transcriber],
            options: SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: config.retention.framework))
        do {
            if !config.contextualStrings.isEmpty {
                let context = AnalysisContext()
                context.contextualStrings[.general] = config.contextualStrings
                try await analyzer.setContext(context)
            }
            try await analyzer.prepareToAnalyze(in: format)
        } catch {
            await analyzer.cancelAndFinishNow()
            throw error
        }
        return Built(analyzer: analyzer, transcriber: transcriber, format: format)
    }

    func start(
        onResult: @escaping @Sendable (RecognizerResult) -> Void,
        onFailure: @escaping @Sendable (EngineError) -> Void
    ) async throws {
        guard let (analyzer, transcriber) = parts.withLock({ p in p.analyzer.flatMap { a in p.transcriber.map { (a, $0) } } })
        else {
            throw EngineError(LK_SPEECH_ERR_INTERNAL, "start before prepare")
        }
        let (stream, input) = AsyncStream.makeStream(of: AnalyzerInput.self, bufferingPolicy: .unbounded)
        let results = Task.detached(priority: .userInitiated) { [weak self] in
            do {
                for try await r in transcriber.results {
                    guard let self, !self.isCancelled else { return }
                    onResult(Self.result(r))
                }
            } catch {
                guard let self, !self.isCancelled, !(error is CancellationError) else { return }
                onFailure(ErrorMapping.map(error))
            }
        }
        parts.withLock { p in
            p.input = input
            p.results = results
        }
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            input.finish()
            results.cancel()
            throw ErrorMapping.map(error)
        }
    }

    func feed(_ buffer: AVAudioPCMBuffer) {
        let input = parts.withLock { $0.input }
        input?.yield(AnalyzerInput(buffer: buffer))
    }

    func finish() async throws {
        let (analyzer, input, results) = parts.withLock { p in (p.analyzer, p.input, p.results) }
        input?.finish()
        guard let analyzer else { throw EngineError(LK_SPEECH_ERR_INTERNAL, "finish before prepare") }
        do {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        } catch {
            throw ErrorMapping.map(error)
        }
        // The results stream ends once the analyzer has finished; wait until every result is out.
        await results?.value
        release()
    }

    func cancel() async {
        let (analyzer, input, results) = parts.withLock { p in
            p.cancelled = true
            return (p.analyzer, p.input, p.results)
        }
        input?.finish()
        results?.cancel()
        await analyzer?.cancelAndFinishNow()
        release()
    }

    /// Drops every reference, so the analyzer and the buffers it holds are freed.
    private func release() {
        parts.withLock { p in
            p.input = nil
            p.results = nil
            p.analyzer = nil
            p.transcriber = nil
        }
    }

    private static func result(_ r: DictationTranscriber.Result) -> RecognizerResult {
        RecognizerResult(
            text: String(r.text.characters),
            start: r.range.start.seconds,
            end: r.range.end.seconds,
            finalizedThrough: r.resultsFinalizationTime.seconds,
            isFinal: r.isFinal)
    }
}

extension ModelRetention {
    var framework: SpeechAnalyzer.Options.ModelRetention {
        switch self {
        case .whileInUse: .whileInUse
        case .lingering: .lingering
        case .processLifetime: .processLifetime
        }
    }
}

/// Maps Speech framework errors to the C ABI's codes (include/lookout_speech.h).
package enum ErrorMapping {
    package static func map(_ error: any Error) -> EngineError {
        if let e = error as? EngineError { return e }
        if error is CancellationError { return EngineError(LK_SPEECH_ERR_ENGINE, "the recognizer stopped (cancelled)") }
        if let e = error as? SFSpeechError {
            let what = describe(error)
            switch e.code {
            case .noModel:
                return EngineError(LK_SPEECH_ASSET_NOT_INSTALLED, what)
            case .cannotAllocateUnsupportedLocale:
                return EngineError(LK_SPEECH_LOCALE_UNSUPPORTED, what)
            case .assetLocaleNotAllocated, .tooManyAssetLocalesAllocated:
                return EngineError(LK_SPEECH_ERR_ASSET_RESERVATION, what)
            case .insufficientResources:
                return EngineError(LK_SPEECH_ERR_RESOURCES, what)
            case .audioDisordered, .unexpectedAudioFormat, .incompatibleAudioFormats:
                return EngineError(LK_SPEECH_ERR_AUDIO_FORMAT, what)
            case .timeout:
                return EngineError(LK_SPEECH_ERR_TIMEOUT, what)
            default:
                return EngineError(LK_SPEECH_ERR_ENGINE, what)
            }
        }
        return EngineError(LK_SPEECH_ERR_ENGINE, describe(error))
    }

    /// SpeechAnalyzer's "model installed but not reserved for this app" error.
    package static func isUnreservedLocale(_ error: any Error) -> Bool {
        (error as? SFSpeechError)?.code == .assetLocaleNotAllocated
    }

    /// "domain code: description", for ERROR events. Framework messages carry no transcript.
    package static func describe(_ error: any Error) -> String {
        if let e = error as? EngineError { return e.detail }
        let ns = error as NSError
        return "\(ns.domain) \(ns.code): \(ns.localizedDescription)"
    }
}
