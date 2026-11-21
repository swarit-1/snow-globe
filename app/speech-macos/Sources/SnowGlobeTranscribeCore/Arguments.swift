import Foundation
import SnowGlobeSpeechC

/// snow-globe-transcribe's command line.
public enum Command: Equatable, Sendable {
    case transcribe(path: String, options: ReplayOptions)
    case status(locale: String)
    case installAssets(locale: String, confirmed: Bool)
    case version
    case help
}

public struct UsageError: Error, Equatable, CustomStringConvertible {
    public let description: String
    public init(_ d: String) { description = d }
}

public enum Arguments {
    public static let usage = """
        snow-globe-transcribe: replay a WAV file through Snow Globe's on-device speech library
        (app/speech-macos, the C ABI in include/snow_globe_speech.h) and print one JSON line per event.

        usage:
          snow-globe-transcribe [options] FILE.wav
          snow-globe-transcribe status [--locale L]
          snow-globe-transcribe install-assets --locale L --yes
          snow-globe-transcribe version

        transcribe options:
          --locale L          BCP 47 locale (default en-US)
          --chunk-ms N        audio per push, in ms (default 10, like a microphone callback)
          --fast              push at up to 20x the audio's pace (default: its own pace), waiting
                              for READY after the first 15 s: the library's ring holds 20 s and
                              nothing drains it before the recognizer is ready
          --no-text           omit transcript text; keep lengths and word counts (PRD §10.5)
          --context S         a contextual string such as a viewpoint name (repeatable)
          --pad-ms N          silence pushed at key-up before finalizing (0-2000; default 0)
          --retention R       model retention: while-in-use, lingering (default), process-lifetime
          --timeout-s N       wait this long for FINAL or ERROR (default 20)

        output (stdout, JSON lines): "meta", then one "event" per callback (ready, partial, stable,
        final, error) with host_ns (CLOCK_UPTIME_RAW), wall_s since sg_speech_start, audio_pushed_s,
        and the event's audio_start_s/audio_end_s; then "summary" with finish_to_terminal_ms and push
        timing. The timings are diagnostics: there is no microphone, cpal or device stage, so they
        are not D-004's release-to-final measure or the PRD 10.6 replay (that is the M1 harness).
        Exit status: 0 FINAL, 1 ERROR or no terminal event, 2 usage, 3 unreadable WAV, 4 not ready.

        status prints sg_speech_status for the locale. install-assets downloads Apple's on-device
        dictation model for the locale from Apple's servers (the library itself never downloads
        anything) and needs --yes. Neither records audio.
        """

    public static func parse(_ argv: [String]) throws -> Command {
        var args = argv[...]
        guard let first = args.first else { return .help }
        switch first {
        case "-h", "--help", "help":
            return .help
        case "version", "--version":
            return .version
        case "status":
            args = args.dropFirst()
            var locale = "en-US"
            while let a = args.popFirst() {
                switch a {
                case "--locale": locale = try value(&args, a)
                default: throw UsageError("status: unknown argument \(a)")
                }
            }
            return .status(locale: locale)
        case "install-assets":
            args = args.dropFirst()
            var locale: String?
            var yes = false
            while let a = args.popFirst() {
                switch a {
                case "--locale": locale = try value(&args, a)
                case "--yes": yes = true
                default: throw UsageError("install-assets: unknown argument \(a)")
                }
            }
            guard let locale else { throw UsageError("install-assets needs --locale") }
            return .installAssets(locale: locale, confirmed: yes)
        default:
            break
        }
        var o = ReplayOptions()
        var path: String?
        while let a = args.popFirst() {
            switch a {
            case "--locale": o.locale = try value(&args, a)
            case "--chunk-ms":
                guard let v = Double(try value(&args, a)), v >= 0.1, v <= 1_000 else {
                    throw UsageError("--chunk-ms needs a number from 0.1 to 1000")
                }
                o.chunkMs = v
            case "--fast": o.realtime = false
            case "--no-text": o.includeText = false
            case "--context": o.contextualStrings.append(try value(&args, a))
            case "--pad-ms":
                guard let v = Int32(try value(&args, a)), (0...2_000).contains(v) else {
                    throw UsageError("--pad-ms needs an integer from 0 to 2000")
                }
                o.silencePadMs = v
            case "--retention":
                let v = try value(&args, a)
                switch v {
                case "while-in-use": o.retention = SG_SPEECH_RETAIN_WHILE_IN_USE
                case "lingering": o.retention = SG_SPEECH_RETAIN_LINGERING
                case "process-lifetime": o.retention = SG_SPEECH_RETAIN_PROCESS_LIFETIME
                default: throw UsageError("--retention must be while-in-use, lingering or process-lifetime")
                }
            case "--timeout-s":
                guard let v = Double(try value(&args, a)), v > 0, v <= 600 else {
                    throw UsageError("--timeout-s needs a number of seconds up to 600")
                }
                o.terminalTimeoutSeconds = v
            default:
                if a.hasPrefix("-") { throw UsageError("unknown option \(a)") }
                guard path == nil else { throw UsageError("only one WAV file at a time") }
                path = a
            }
        }
        guard let path else { throw UsageError("no WAV file given") }
        o.sourceName = (path as NSString).lastPathComponent
        return .transcribe(path: path, options: o)
    }

    private static func value(_ args: inout ArraySlice<String>, _ flag: String) throws -> String {
        guard let v = args.popFirst() else { throw UsageError("\(flag) needs a value") }
        return v
    }
}
