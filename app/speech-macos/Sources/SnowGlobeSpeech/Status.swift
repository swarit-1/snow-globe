import Dispatch
import Foundation
import SnowGlobeSpeechC
import Speech
import Synchronization

/// What `sg_speech_status` learns from the system for one locale.
package struct StatusFacts: Sendable, Equatable {
    package var localeSupported: Bool
    package var authorization: SpeechAuthorization
    package var assets: AssetState

    package init(localeSupported: Bool, authorization: SpeechAuthorization, assets: AssetState) {
        self.localeSupported = localeSupported
        self.authorization = authorization
        self.assets = assets
    }
}

/// Where `sg_speech_status` gets its facts. Package access: the tests substitute a fake, because
/// the real queries go to the Speech framework.
package protocol StatusProbe: Sendable {
    func facts(locale: String) async -> StatusFacts
}

/// The real probe: DictationTranscriber's locale list, the current authorization (read without
/// prompting) and AssetInventory's status for the same module configuration the sessions use.
struct FrameworkStatusProbe: StatusProbe {
    func facts(locale: String) async -> StatusFacts {
        let auth = SpeechAuthorization.current()
        guard let loc = await DictationTranscriber.supportedLocale(equivalentTo: Locale(identifier: locale)) else {
            return StatusFacts(localeSupported: false, authorization: auth, assets: .unsupported)
        }
        let state = await AssetInventory.status(forModules: [TranscriberSetup.make(locale: loc)])
        return StatusFacts(localeSupported: true, authorization: auth, assets: AssetState(state))
    }
}

package enum StatusProbes {
    package static let real: any StatusProbe = FrameworkStatusProbe()
    package static let current = Mutex<any StatusProbe>(real)
}

/// The status code for a set of facts (include/snow_globe_speech.h): an unsupported locale first,
/// then a refused authorization, then a missing model.
package enum StatusMapping {
    package static func code(_ f: StatusFacts) -> Int32 {
        if !f.localeSupported || f.assets == .unsupported { return SG_SPEECH_LOCALE_UNSUPPORTED }
        if f.authorization.blocks { return SG_SPEECH_NOT_AUTHORIZED }
        if f.assets != .installed { return SG_SPEECH_ASSET_NOT_INSTALLED }
        return SG_SPEECH_OK
    }
}

/// Runs `operation` on the cooperative pool and blocks the calling thread until it returns, or
/// until `seconds` pass (then nil). For the C ABI's synchronous `sg_speech_status`; never call it
/// from a Swift concurrency task.
func blockingAwait<T: Sendable>(seconds: Double, _ operation: @escaping @Sendable () async -> T) -> T? {
    let box = ResultBox<T>()
    let done = DispatchSemaphore(value: 0)
    Task.detached(priority: .userInitiated) {
        box.set(await operation())
        done.signal()
    }
    guard done.wait(timeout: .now() + seconds) == .success else { return nil }
    return box.get()
}

private final class ResultBox<T: Sendable>: Sendable {
    private let value = Mutex<T?>(nil)
    func set(_ v: T) { value.withLock { $0 = v } }
    func get() -> T? { value.withLock { $0 } }
}
