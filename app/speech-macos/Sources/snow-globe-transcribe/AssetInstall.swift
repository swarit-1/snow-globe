import Foundation
import SnowGlobeSpeech
import Speech

/// `snow-globe-transcribe install-assets`: downloads Apple's on-device dictation model for a locale.
///
/// This is the only code in the package that can make the system fetch anything, and it lives in
/// the CLI, not in libsnow_globe_speech.a (scripts/check-symbols.sh checks the archive never refers
/// to AssetInventory's download API). Only the owner runs it, on purpose, with --yes: the model
/// comes from Apple's servers ("These assets are machine-learning models downloaded from Apple's
/// servers and managed by the system", AssetInventory documentation). No audio is involved.
///
/// It reserves the locale for this tool's identity (dev.snowglobe.transcribe) as part of the
/// installation; other apps (the M1 spike, the shell) get their own reservation the first time a
/// session needs one (see AssetReservation in the library).
///
/// TODO(M1): which identity AssetInventory (and TCC) key reservations on is unverified: the
/// embedded Info.plist's bundle identifier or the code-signing identifier. scripts/build.sh signs
/// this tool ad hoc as dev.snowglobe.transcribe, so for it the two agree. The reservation lines
/// below (`status` prints one; `install-assets` prints one before and one after) are the evidence
/// for the owner's first run.
enum AssetInstall {
    static func run(locale tag: String, confirmed: Bool) async -> Int32 {
        guard confirmed else {
            FileHandle.standardError.write(Data("""
                install-assets downloads Apple's on-device dictation model for \(tag) from Apple's
                servers. Run it again with --yes to go ahead.

                """.utf8))
            return 2
        }
        guard let locale = await DictationTranscriber.supportedLocale(equivalentTo: Locale(identifier: tag)) else {
            print("{\"type\":\"install\",\"locale\":\"\(tag)\",\"result\":\"locale-unsupported\"}")
            return 4
        }
        // The library's own configuration, so the model installed here is the one sessions load.
        let module = TranscriberSetup.make(locale: locale)
        print(await reservationsLine(when: "before"))
        let rc = await install(module, tag: tag)
        print(await reservationsLine(when: "after"))
        return rc
    }

    private static func install(_ module: DictationTranscriber, tag: String) async -> Int32 {
        let before = await AssetInventory.status(forModules: [module])
        print("{\"type\":\"install\",\"locale\":\"\(tag)\",\"status_before\":\"\(name(before))\"}")
        if before == .installed { return 0 }
        do {
            guard let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) else {
                print("{\"type\":\"install\",\"result\":\"nothing-to-install\"}")
                return 0
            }
            let progress = request.progress
            let reporter = Task {
                while !Task.isCancelled {
                    print("{\"type\":\"progress\",\"fraction\":\(progress.fractionCompleted)}")
                    try? await Task.sleep(for: .seconds(1))
                }
            }
            defer { reporter.cancel() }
            try await request.downloadAndInstall()
        } catch {
            let ns = error as NSError
            print("{\"type\":\"install\",\"result\":\"error\",\"domain\":\"\(ns.domain)\",\"code\":\(ns.code)}")
            return 1
        }
        let after = await AssetInventory.status(forModules: [module])
        print("{\"type\":\"install\",\"status_after\":\"\(name(after))\"}")
        return after == .installed ? 0 : 1
    }

    /// This tool's model reservations, as one JSON line: AssetInventory.reservedLocales (this
    /// process's identity) and maximumReservedLocales. A query only: it reserves, releases and
    /// downloads nothing.
    static func reservationsLine(when: String) async -> String {
        let reserved = await AssetInventory.reservedLocales.map(\.identifier).sorted()
        let data = try? JSONSerialization.data(
            withJSONObject: [
                "type": "reservations", "when": when, "reserved_locales": reserved,
                "maximum": AssetInventory.maximumReservedLocales,
            ] as [String: Any],
            options: [.sortedKeys])
        return data.flatMap { String(data: $0, encoding: .utf8) } ?? "{\"type\":\"reservations\",\"error\":\"encode\"}"
    }

    static func name(_ s: AssetInventory.Status) -> String {
        switch s {
        case .unsupported: "unsupported"
        case .supported: "supported"
        case .downloading: "downloading"
        case .installed: "installed"
        @unknown default: "unknown"
        }
    }
}
