// snow-globe-transcribe: WAV in, JSON lines of speech events out (PRD §7.1, §10.5, §10.6).
//
// Replays a WAV file through libsnow_globe_speech's C ABI, the same calls the Tauri shell makes, and
// prints every event with its wall and audio times. Voice fixtures come only from the owner
// (CLAUDE.md); this tool never records audio and never writes any.

import Foundation
import SnowGlobeSpeechC
import SnowGlobeTranscribeCore

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("snow-globe-transcribe: \(message)\n".utf8))
    exit(code)
}

let command: Command
do {
    command = try Arguments.parse(Array(CommandLine.arguments.dropFirst()))
} catch {
    FileHandle.standardError.write(Data("snow-globe-transcribe: \(error)\n\n\(Arguments.usage)\n".utf8))
    exit(2)
}

switch command {
case .help:
    print(Arguments.usage)
case .version:
    print(String(cString: sg_speech_version()))
case .status(let locale):
    let code = locale.withCString { sg_speech_status($0) }
    print(StatusText.statusLine(locale: locale, code: code))
    // This tool's model reservations (evidence for the reservation-identity TODO(M1)).
    print(await AssetInstall.reservationsLine(when: "status"))
    exit(code == SG_SPEECH_OK ? 0 : 4)
case .installAssets(let locale, let confirmed):
    exit(await AssetInstall.run(locale: locale, confirmed: confirmed))
case .transcribe(let path, let options):
    let audio: WAVAudio
    do {
        audio = try WAVReader.read(path: path)
    } catch {
        fail("\(options.sourceName): \(error)", 3)
    }
    let status = options.locale.withCString { sg_speech_status($0) }
    guard status == SG_SPEECH_OK else {
        print(StatusText.statusLine(locale: options.locale, code: status))
        exit(4)
    }
    let outcome = Replay.run(audio: audio, options: options)
    for line in outcome.lines { print(line) }
    exit(outcome.exitCode)
}
