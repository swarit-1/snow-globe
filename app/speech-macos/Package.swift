// swift-tools-version: 6.2
//
// Lookout's speech library (PRD §6.1, §6.5; engine A1 of §7.1, D-015): SpeechAnalyzer with
// DictationTranscriber, on device only, behind the C ABI in include/lookout_speech.h.
//
// Build output belongs outside the iCloud-synced repo (CLAUDE.md), so always pass a scratch path:
//   swift build -c release --scratch-path "${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
//   swift test --scratch-path "${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
// The static library is then <scratch>/release/liblookout_speech.a, where the M1 spike's build.rs
// looks for it. scripts/build.sh does both and checks the archive's symbols.

import PackageDescription

/// Embedded into lookout-transcribe so the tool has its own bundle identifier and usage string.
let transcribeInfoPlist = "\(Context.packageDirectory)/Sources/lookout-transcribe/Info.plist"

let strictSwift: [SwiftSetting] = [.treatAllWarnings(as: .error)]

let package = Package(
    name: "LookoutSpeech",
    platforms: [.macOS(.v26)],
    products: [
        // liblookout_speech.a: the Swift static library the Tauri shell and the M1 spike link.
        .library(name: "lookout_speech", type: .static, targets: ["LookoutSpeech"]),
        // WAV in, JSONL of events out (bench and owner use; never run on speech by an agent).
        .executable(name: "lookout-transcribe", targets: ["lookout-transcribe"]),
    ],
    targets: [
        // The public header as a Clang module, plus lk_speech_version() and static checks.
        .target(
            name: "LookoutSpeechC",
            cSettings: [.treatAllWarnings(as: .error)]
        ),
        // The library: C ABI entry points, sessions, ring buffer, conversion, the engine.
        .target(
            name: "LookoutSpeech",
            dependencies: ["LookoutSpeechC"],
            swiftSettings: strictSwift,
            linkerSettings: [
                .linkedFramework("Foundation"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("Speech"),
            ]
        ),
        // lookout-transcribe's logic (WAV reader, replay through the C ABI, JSONL), testable
        // without the recognizer.
        .target(
            name: "LookoutTranscribeCore",
            dependencies: ["LookoutSpeechC", "LookoutSpeech"],
            swiftSettings: strictSwift
        ),
        .executableTarget(
            name: "lookout-transcribe",
            dependencies: ["LookoutTranscribeCore", "LookoutSpeech", "LookoutSpeechC"],
            exclude: ["Info.plist"],
            swiftSettings: strictSwift,
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist", "-Xlinker", transcribeInfoPlist,
                ]),
            ]
        ),
        // The scripted recognizer the tests put behind the engine seam. Used only by the test
        // targets; it is in no product, so it never reaches liblookout_speech.a or the CLI.
        .target(
            name: "LookoutSpeechMocks",
            dependencies: ["LookoutSpeech", "LookoutSpeechC"],
            path: "Tests/LookoutSpeechMocks",
            swiftSettings: strictSwift
        ),
        // Test-only: a C initializer that installs the trap engine for the whole test process when
        // the test bundle loads (README, "What the tests need"). In no product.
        .target(
            name: "LookoutSpeechTestBootstrap",
            dependencies: ["LookoutSpeechMocks"],
            path: "Tests/LookoutSpeechTestBootstrap",
            cSettings: [.treatAllWarnings(as: .error)]
        ),
        .testTarget(
            name: "LookoutSpeechTests",
            dependencies: ["LookoutSpeech", "LookoutSpeechC", "LookoutSpeechMocks", "LookoutSpeechTestBootstrap"],
            swiftSettings: strictSwift
        ),
        .testTarget(
            name: "LookoutTranscribeCoreTests",
            dependencies: [
                "LookoutTranscribeCore", "LookoutSpeech", "LookoutSpeechC", "LookoutSpeechMocks",
                "LookoutSpeechTestBootstrap",
            ],
            swiftSettings: strictSwift
        ),
    ],
    swiftLanguageModes: [.v6]
)
