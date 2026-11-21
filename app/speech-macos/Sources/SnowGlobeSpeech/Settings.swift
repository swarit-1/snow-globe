import SnowGlobeSpeechC
import Synchronization

/// How long the recognizer's model stays loaded after a session (`SG_SPEECH_RETAIN_*`), mapped to
/// `SpeechAnalyzer.Options.ModelRetention` by the engine. PRD §10.1 and §10.6 measure idle,
/// warm and cold latency, so this is a setting the bake-off can vary, not a fixed choice.
package enum ModelRetention: Int32, Sendable, Equatable, CaseIterable {
    case whileInUse = 0
    case lingering = 1
    case processLifetime = 2

    /// The C constant's name, for logs.
    package var name: String {
        switch self {
        case .whileInUse: "while-in-use"
        case .lingering: "lingering"
        case .processLifetime: "process-lifetime"
        }
    }
}

/// The process-wide settings a session copies when it starts (PRD §7.1).
///
/// They change only through the `sg_speech_set_*` functions; the library reads no environment
/// variables and no files, so a measured run's settings are exactly what its caller set and logged.
package struct SessionSettings: Sendable, Equatable {
    /// Viewpoint names and aliases, passed to the analyzer as contextual strings (PRD §7.1:
    /// "Viewpoint aliases go in as contextual strings (on device)").
    package var contextualStrings: [String] = []
    /// Silence pushed as one burst at key-up before finalizing (PRD §7.1 finalization lever 1).
    /// Off by default: the lever is to be measured, not assumed.
    package var silencePadMs: Int32 = 0
    package var retention: ModelRetention = .lingering
    /// Seconds after `sg_speech_finish` before the session gives up with `SG_SPEECH_ERR_TIMEOUT`.
    /// Not settable through the C ABI; the tests shorten it.
    package var finishTimeoutSeconds: Double = 15

    package static let maxContextualStrings = 1_000
    package static let maxSilencePadMs: Int32 = 2_000

    package init() {}
}

/// The current process-wide settings.
package enum Settings {
    package static let current = Mutex(SessionSettings())

    package static func snapshot() -> SessionSettings {
        current.withLock { $0 }
    }
}
