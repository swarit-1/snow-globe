import Foundation
import LookoutSpeechC
import Synchronization

// The C ABI (include/lookout_speech.h; PRD §6.5 "Swift in-process static library, C ABI").
//
// Each function is exported under its C name with @_cdecl. The compiler does NOT check these
// signatures against the header's prototypes: only `lk_speech_cb` and the constants come from
// the header (the LookoutSpeechC module), and a drifted prototype would still build this file.
// The check is in the tests: ABITests.testEntryPointsHaveTheHeadersExactTypes assigns each
// Swift entry point to a variable typed from the header's declaration, so the test target fails
// to compile, with a plain type error, when a prototype and its entry point differ (nullability
// aside, which does not change the C ABI). The Swift-side names end in "Entry" so this module
// never sees two declarations of one symbol. lk_speech_version is in
// Sources/LookoutSpeechC/version.c.
//
// A session pointer is an `Unmanaged<Session>` retained once by lk_speech_start and released by
// lk_speech_free.

/// A BCP 47 tag the library accepts: 2 to 64 ASCII letters, digits, '-' or '_', valid UTF-8.
func validLocale(_ p: UnsafePointer<CChar>?) -> String? {
    guard let p, let s = String(validatingCString: p) else { return nil }
    guard (2...64).contains(s.utf8.count) else { return nil }
    let ok = s.utf8.allSatisfy { b in
        (b >= 0x30 && b <= 0x39) || (b >= 0x41 && b <= 0x5A) || (b >= 0x61 && b <= 0x7A) || b == 0x2D || b == 0x5F
    }
    return ok ? s : nil
}

/// The sample rates `lk_speech_start` accepts.
let acceptedSampleRates = 8_000.0...384_000.0

/// How long `lk_speech_status` waits for the system before returning `LK_SPEECH_ERR_TIMEOUT`
/// (10 s; the tests shorten it).
package let statusTimeout = Mutex<Double>(10)

@inline(__always)
private func session(_ s: OpaquePointer) -> Unmanaged<Session> {
    Unmanaged<Session>.fromOpaque(UnsafeRawPointer(s))
}

@_cdecl("lk_speech_status")
public func lkSpeechStatusEntry(_ locale: UnsafePointer<CChar>?) -> Int32 {
    guard let tag = validLocale(locale) else { return LK_SPEECH_ERR_ARGUMENT }
    let probe = StatusProbes.current.withLock { $0 }
    let timeout = statusTimeout.withLock { $0 }
    guard let facts = blockingAwait(seconds: timeout, { await probe.facts(locale: tag) }) else {
        return LK_SPEECH_ERR_TIMEOUT
    }
    return StatusMapping.code(facts)
}

@_cdecl("lk_speech_start")
public func lkSpeechStartEntry(
    _ locale: UnsafePointer<CChar>?, _ sampleRateHz: Double, _ cb: lk_speech_cb?, _ ctx: UnsafeMutableRawPointer?
) -> OpaquePointer? {
    guard let tag = validLocale(locale), let cb, acceptedSampleRates.contains(sampleRateHz) else { return nil }
    let s = Session(
        locale: tag, sampleRate: sampleRateHz, target: CallbackTarget(function: cb, context: ctx),
        settings: Settings.snapshot(), engine: EngineFactory.make())
    s.start()
    return OpaquePointer(Unmanaged.passRetained(s).toOpaque())
}

/// The audio thread's entry point: no retain or release (`_withUnsafeGuaranteedRef`), no lock,
/// no allocation.
@_cdecl("lk_speech_push_f32")
public func lkSpeechPushF32Entry(_ s: OpaquePointer?, _ mono: UnsafePointer<Float>?, _ frames: Int) -> Int32 {
    guard let s, frames >= 0 else { return LK_SPEECH_ERR_ARGUMENT }
    if frames == 0 { return LK_SPEECH_OK }
    guard let mono else { return LK_SPEECH_ERR_ARGUMENT }
    return session(s)._withUnsafeGuaranteedRef { $0.push(mono, frames) }
}

@_cdecl("lk_speech_finish")
public func lkSpeechFinishEntry(_ s: OpaquePointer?) -> Int32 {
    guard let s else { return LK_SPEECH_ERR_ARGUMENT }
    return session(s)._withUnsafeGuaranteedRef { $0.finish() }
}

@_cdecl("lk_speech_cancel")
public func lkSpeechCancelEntry(_ s: OpaquePointer?) {
    guard let s else { return }
    session(s)._withUnsafeGuaranteedRef { $0.cancel() }
}

@_cdecl("lk_speech_free")
public func lkSpeechFreeEntry(_ s: OpaquePointer?) {
    guard let s else { return }
    let u = session(s)
    u._withUnsafeGuaranteedRef { $0.close() }
    u.release()
}

@_cdecl("lk_speech_set_contextual_strings")
public func lkSpeechSetContextualStringsEntry(
    _ strings: UnsafePointer<UnsafePointer<CChar>?>?, _ count: Int
) -> Int32 {
    guard count >= 0, count <= SessionSettings.maxContextualStrings else { return LK_SPEECH_ERR_ARGUMENT }
    var list: [String] = []
    if count > 0 {
        guard let strings else { return LK_SPEECH_ERR_ARGUMENT }
        list.reserveCapacity(count)
        for i in 0..<count {
            guard let p = strings[i], let s = String(validatingCString: p),
                !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { return LK_SPEECH_ERR_ARGUMENT }
            list.append(s)
        }
    }
    Settings.current.withLock { $0.contextualStrings = list }
    return LK_SPEECH_OK
}

@_cdecl("lk_speech_set_silence_pad_ms")
public func lkSpeechSetSilencePadMsEntry(_ ms: Int32) -> Int32 {
    guard (0...SessionSettings.maxSilencePadMs).contains(ms) else { return LK_SPEECH_ERR_ARGUMENT }
    Settings.current.withLock { $0.silencePadMs = ms }
    return LK_SPEECH_OK
}

@_cdecl("lk_speech_set_model_retention")
public func lkSpeechSetModelRetentionEntry(_ retention: Int32) -> Int32 {
    guard let r = ModelRetention(rawValue: retention) else { return LK_SPEECH_ERR_ARGUMENT }
    Settings.current.withLock { $0.retention = r }
    return LK_SPEECH_OK
}
