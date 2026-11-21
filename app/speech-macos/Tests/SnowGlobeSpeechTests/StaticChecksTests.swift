import Foundation
import XCTest

/// Static checks on what the library is made of (PRD §15.1: on device only; §6.5: the C ABI).
///
/// - The header compiles as strict C11 and C++17, and declares exactly the prototypes the M1
///   spike's FFI was written against (tauri-app/src/speech.rs checks the same list).
/// - The library's compiled objects refer to no networking API, no server-capable recognition
///   API, no model download API, no microphone or recording API, nothing that writes or reads
///   files, and never `requestAuthorization` (which prompts). The rules are
///   scripts/forbidden-symbols.txt, which scripts/check-symbols.sh also applies to the release
///   archive (with the reviewed import baseline on top).
final class StaticChecksTests: XCTestCase {
    private var header: URL { packageRoot.appendingPathComponent("include/snow_globe_speech.h") }

    @discardableResult
    private func run(_ tool: String, _ args: [String]) throws -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        try p.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    func testHeaderCompilesAsStrictCAndCPlusPlus() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("sg-header-\(getpid())")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        // Each function is assigned to a pointer of the type the spike's Rust FFI uses, so a
        // drifted parameter type fails to compile.
        let probe = """
            #include "\(header.path)"
            int32_t (*f_status)(const char *) = sg_speech_status;
            sg_speech *(*f_start)(const char *, double, sg_speech_cb, void *) = sg_speech_start;
            int32_t (*f_push)(sg_speech *, const float *, size_t) = sg_speech_push_f32;
            int32_t (*f_finish)(sg_speech *) = sg_speech_finish;
            void (*f_cancel)(sg_speech *) = sg_speech_cancel;
            void (*f_free)(sg_speech *) = sg_speech_free;
            const char *(*f_version)(void) = sg_speech_version;
            int32_t (*f_ctx)(const char *const *, size_t) = sg_speech_set_contextual_strings;
            int32_t (*f_pad)(int32_t) = sg_speech_set_silence_pad_ms;
            int32_t (*f_ret)(int32_t) = sg_speech_set_model_retention;
            void cb(void *ctx, sg_event_kind kind, const char *text, double a, double b, int32_t e);
            sg_speech_cb f_cb = cb;
            int codes[] = { SG_SPEECH_OK, SG_SPEECH_ASSET_NOT_INSTALLED, SG_SPEECH_NOT_AUTHORIZED,
                            SG_SPEECH_LOCALE_UNSUPPORTED, SG_SPEECH_ERR_ARGUMENT, SG_SPEECH_ERR_STATE,
                            SG_SPEECH_ERR_OVERFLOW, SG_SPEECH_ERR_AUDIO_FORMAT, SG_SPEECH_ERR_ENGINE,
                            SG_SPEECH_ERR_TIMEOUT, SG_SPEECH_ERR_RESOURCES, SG_SPEECH_ERR_ASSET_RESERVATION,
                            SG_SPEECH_ERR_INTERNAL, SG_SPEECH_RETAIN_WHILE_IN_USE, SG_SPEECH_RETAIN_LINGERING,
                            SG_SPEECH_RETAIN_PROCESS_LIFETIME, SG_EV_READY, SG_EV_PARTIAL, SG_EV_FINAL, SG_EV_ERROR,
                            SG_EV_STABLE };
            """
        for (ext, flags) in [("c", ["-x", "c", "-std=c11"]), ("cpp", ["-x", "c++", "-std=c++17"])] {
            let src = dir.appendingPathComponent("probe.\(ext)")
            try (probe + "\n").write(to: src, atomically: true, encoding: .utf8)
            let (status, out) = try run(
                "/usr/bin/xcrun",
                ["clang"] + flags + ["-Wall", "-Wextra", "-Werror", "-pedantic", "-fsyntax-only", src.path])
            XCTAssertEqual(status, 0, "\(ext): \(out)")
        }
    }

    func testHeaderDeclaresTheSpikesPrototypesExactly() throws {
        let squash = { (s: String) in s.filter { !$0.isWhitespace } }
        let h = squash(try String(contentsOf: header, encoding: .utf8))
        // The list in spikes/m1-platform/tauri-app/src/speech.rs, header_matches_the_abi_when_present.
        for proto in [
            "int32_t sg_speech_status(const char *locale_bcp47);",
            "sg_speech *sg_speech_start(const char *locale_bcp47, double sample_rate_hz, sg_speech_cb cb, void *ctx);",
            "int32_t sg_speech_push_f32(sg_speech *s, const float *mono, size_t frames);",
            "int32_t sg_speech_finish(sg_speech *s);",
            "void sg_speech_cancel(sg_speech *s);",
            "void sg_speech_free(sg_speech *s);",
            "const char *sg_speech_version(void);",
            "typedef void (*sg_speech_cb)(void *ctx, sg_event_kind kind, const char *utf8_text, double audio_start_s, double audio_end_s, int32_t error_code);",
            "SG_EV_READY = 0", "SG_EV_PARTIAL = 1", "SG_EV_FINAL = 2", "SG_EV_ERROR = 3",
        ] {
            XCTAssertTrue(h.contains(squash(proto)), "header lacks \(proto)")
        }
    }

    /// The library's object files from this test build (the same sources as the release archive).
    private func libraryObjects() throws -> [URL] {
        let bundle = Bundle(for: Self.self).bundleURL
        let buildDir = bundle.deletingLastPathComponent()
        var objects: [URL] = []
        for target in ["SnowGlobeSpeech.build", "SnowGlobeSpeechC.build"] {
            let dir = buildDir.appendingPathComponent(target)
            let files = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            objects += files.filter { $0.pathExtension == "o" }
        }
        return objects
    }

    /// The deny list, read from scripts/forbidden-symbols.txt, the file check-symbols.sh reads, so
    /// the two checks cannot drift apart.
    static func forbidden() throws -> [(what: String, pattern: NSRegularExpression)] {
        let text = try String(contentsOf: packageRoot.appendingPathComponent("scripts/forbidden-symbols.txt"), encoding: .utf8)
        return try text.split(separator: "\n").compactMap { line in
            guard !line.hasPrefix("#"), !line.isEmpty else { return nil }
            let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { throw CocoaError(.fileReadCorruptFile) }
            return (parts[0], try NSRegularExpression(pattern: parts[1]))
        }
    }

    private static func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    /// The deny list against real names: those a forbidden call produces must each match a rule,
    /// and harmless names the library does use must match none. The Swift names were taken from
    /// `nm -u` on objects compiled with this toolchain (Swift 6.2.3, SDK 26.2) from one-line
    /// calls of each API; selectors are checked as the script checks them, @selector(name).
    func testDenyListRejectsForbiddenNamesAndAcceptsHarmlessOnes() throws {
        let rules = try Self.forbidden()
        XCTAssertGreaterThanOrEqual(rules.count, 10)
        let forbiddenNames = [
            "_$s10Foundation4DataV5write2to7optionsyAA3URLV_So20NSDataWritingOptionsVtKF",  // Data.write(to:)
            "_$s10Foundation4DataV10contentsOf7optionsAcA3URLVh_So20NSDataReadingOptionsVtKcfC",  // Data(contentsOf:)
            "_$sSy10FoundationE5write6toFile10atomically8encodingyqd___SbSSAAE8EncodingVtKSyRd__lF",  // String.write(toFile:)
            "_$s6Darwin4openys5Int32VSPys4Int8VG_ADtF",  // open(_:_:)
            "_$s10Foundation10URLRequestV3url11cachePolicy15timeoutIntervalAcA3URLV_So017NSURLRequestCacheE0VSdtcfC",
            "_$s7Network12NWConnectionC4host4port5usingAcA10NWEndpointO4HostO_AH4PortVAA12NWParametersCtcfC",
            "_$s6Speech14AssetInventoryC24assetInstallationRequest10supportingAA0beF0CSgSayAA0A6Module_pG_tYaKFZ",
            "_OBJC_CLASS_$_NSFileManager", "_OBJC_CLASS_$_NSURLSession", "_OBJC_CLASS_$_AVAudioEngine",
            "_OBJC_CLASS_$_SFSpeechAudioBufferRecognitionRequest",
            "_open", "_write", "_fopen", "_unlink", "_socket", "_getaddrinfo",
            "_CFWriteStreamCreateWithFile", "_CFReadStreamCreateWithFile",
            "@selector(writeToFile:atomically:)", "@selector(initWithContentsOfURL:)", "@selector(requestAuthorization:)",
            "@selector(recognitionTaskWithRequest:resultHandler:)", "@selector(createFileAtPath:contents:attributes:)",
        ]
        for name in forbiddenNames {
            XCTAssertTrue(rules.contains { Self.matches($0.pattern, name) }, "no rule rejects \(name)")
        }
        let harmless = [
            "_$sSd5write2toyxz_ts16TextOutputStreamRzlF",  // Double.write(to: inout TextOutputStream)
            "_$s15SnowGlobeSpeech9FloatRingC5write_5countSbSPySfG_SitF",  // the library's own ring
            "_swift_isEscapingClosureAtFileLocation", "_dispatch_semaphore_create", "_memset_s", "_memcpy",
            "_$sSa6append10contentsOfyqd__n_t7ElementQyd__RszSTRd__lF",  // Array.append(contentsOf:)
            "@selector(convertToBuffer:error:withInputFromBlock:)", "@selector(authorizationStatus)",
            "_OBJC_CLASS_$_SFSpeechRecognizer", "_OBJC_CLASS_$_AVAudioConverter", "_OBJC_CLASS_$_AVAudioPCMBuffer",
        ]
        for name in harmless {
            let hits = rules.filter { Self.matches($0.pattern, name) }.map(\.what)
            XCTAssertEqual(hits, [], "\(name) is harmless")
        }
    }

    func testLibraryObjectsReferToNoForbiddenAPI() throws {
        let objects = try libraryObjects()
        XCTAssertGreaterThanOrEqual(objects.count, 9, "found \(objects.map(\.lastPathComponent))")
        var undefined = Set<String>()
        var defined = Set<String>()
        var selectors = Set<String>()
        for o in objects {
            let (s1, u) = try run("/usr/bin/nm", ["-u", "-j", o.path])
            XCTAssertEqual(s1, 0, u)
            undefined.formUnion(u.split(separator: "\n").map(String.init))
            let (s2, d) = try run("/usr/bin/nm", ["-g", "-U", "-j", o.path])
            XCTAssertEqual(s2, 0, d)
            defined.formUnion(d.split(separator: "\n").map(String.init))
            // Objective-C calls leave only a selector name (scripts/check-symbols.sh does the same).
            let (s3, m) = try run("/usr/bin/xcrun", ["otool", "-v", "-s", "__TEXT", "__objc_methname", o.path])
            XCTAssertEqual(s3, 0, m)
            let addressed = try NSRegularExpression(pattern: #"^[0-9a-f]{16}\s+(.+)$"#, options: [.anchorsMatchLines])
            for hit in addressed.matches(in: m, range: NSRange(m.startIndex..., in: m)) {
                if let r = Range(hit.range(at: 1), in: m) { selectors.insert("@selector(\(m[r]))") }
            }
        }
        XCTAssertTrue(selectors.contains("@selector(convertToBuffer:error:withInputFromBlock:)"), "selectors were read: \(selectors.count)")
        undefined.formUnion(selectors)
        for (what, re) in try Self.forbidden() {
            let hits = undefined.filter { Self.matches(re, $0) }
            XCTAssertTrue(hits.isEmpty, "\(what): \(hits.sorted())")
        }
        // Strings too: Objective-C selectors and Swift names live in the objects' string tables.
        for o in objects {
            let bytes = try Data(contentsOf: o)
            for needle in ["recognitionTask", "requestAuthorization", "NSURLSession", "downloadAndInstall",
                           "assetInstallationRequest", "AVAudioFile", "AVAudioEngine"] {
                XCTAssertNil(bytes.range(of: Data(needle.utf8)), "\(o.lastPathComponent) contains \(needle)")
            }
        }
        for f in ["status", "start", "push_f32", "finish", "cancel", "free", "version",
                  "set_contextual_strings", "set_silence_pad_ms", "set_model_retention"] {
            XCTAssertTrue(defined.contains("_sg_speech_\(f)"), "the library defines _sg_speech_\(f)")
        }
        // What the library does refer to from the Speech framework: the analyzer API, and the
        // authorization *status* only.
        XCTAssertTrue(undefined.contains("_OBJC_CLASS_$_SFSpeechRecognizer"), "authorizationStatus() is read")
    }
}
