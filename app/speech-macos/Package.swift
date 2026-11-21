// swift-tools-version: 6.2
//
// Snow Globe's speech library (PRD §6.1, §6.5; engine A1 of §7.1, D-015): SpeechAnalyzer with
// DictationTranscriber, on device only, behind the C ABI in include/snow_globe_speech.h.
//
// Build output belongs outside the iCloud-synced repo (CLAUDE.md), so always pass a scratch path
// (~/lookout-data is the data dir until the rename's live switch, and a symlink to ~/snow-globe-data
// after it, so these never create a second data folder; D-080):
//   swift build -c release --scratch-path "${SNOW_GLOBE_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
//   swift test --scratch-path "${SNOW_GLOBE_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
// The static library is then <scratch>/release/libsnow_globe_speech.a, where the M1 spike's build.rs
// looks for it. scripts/build.sh does both and checks the archive's symbols.

import PackageDescription

/// Embedded into snow-globe-transcribe so the tool has its own bundle identifier and usage string.
let transcribeInfoPlist = "\(Context.packageDirectory)/Sources/snow-globe-transcribe/Info.plist"

let strictSwift: [SwiftSetting] = [.treatAllWarnings(as: .error)]

let package = Package(
    name: "SnowGlobeSpeech",
    platforms: [.macOS(.v26)],
    products: [
        // libsnow_globe_speech.a: the Swift static library the Tauri shell and the M1 spike link.
        .library(name: "snow_globe_speech", type: .static, targets: ["SnowGlobeSpeech"]),
        // WAV in, JSONL of events out (bench and owner use; never run on speech by an agent).
        .executable(name: "snow-globe-transcribe", targets: ["snow-globe-transcribe"]),
    ],
    targets: [
        // The public header as a Clang module, plus sg_speech_version() and static checks.
        .target(
            name: "SnowGlobeSpeechC",
            cSettings: [.treatAllWarnings(as: .error)]
        ),
        // The library: C ABI entry points, sessions, ring buffer, conversion, the engine.
        .target(
            name: "SnowGlobeSpeech",
            dependencies: ["SnowGlobeSpeechC"],
            swiftSettings: strictSwift,
            linkerSettings: [
                .linkedFramework("Foundation"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("Speech"),
            ]
        ),
        // snow-globe-transcribe's logic (WAV reader, replay through the C ABI, JSONL), testable
        // without the recognizer.
        .target(
            name: "SnowGlobeTranscribeCore",
            dependencies: ["SnowGlobeSpeechC", "SnowGlobeSpeech"],
            swiftSettings: strictSwift
        ),
        .executableTarget(
            name: "snow-globe-transcribe",
            dependencies: ["SnowGlobeTranscribeCore", "SnowGlobeSpeech", "SnowGlobeSpeechC"],
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
        // targets; it is in no product, so it never reaches libsnow_globe_speech.a or the CLI.
        .target(
            name: "SnowGlobeSpeechMocks",
            dependencies: ["SnowGlobeSpeech", "SnowGlobeSpeechC"],
            path: "Tests/SnowGlobeSpeechMocks",
            swiftSettings: strictSwift
        ),
        // Test-only: a C initializer that installs the trap engine for the whole test process when
        // the test bundle loads (README, "What the tests need"). In no product.
        .target(
            name: "SnowGlobeSpeechTestBootstrap",
            dependencies: ["SnowGlobeSpeechMocks"],
            path: "Tests/SnowGlobeSpeechTestBootstrap",
            cSettings: [.treatAllWarnings(as: .error)]
        ),
        .testTarget(
            name: "SnowGlobeSpeechTests",
            dependencies: ["SnowGlobeSpeech", "SnowGlobeSpeechC", "SnowGlobeSpeechMocks", "SnowGlobeSpeechTestBootstrap"],
            swiftSettings: strictSwift
        ),
        .testTarget(
            name: "SnowGlobeTranscribeCoreTests",
            dependencies: [
                "SnowGlobeTranscribeCore", "SnowGlobeSpeech", "SnowGlobeSpeechC", "SnowGlobeSpeechMocks",
                "SnowGlobeSpeechTestBootstrap",
            ],
            swiftSettings: strictSwift
        ),
    ],
    swiftLanguageModes: [.v6]
)
