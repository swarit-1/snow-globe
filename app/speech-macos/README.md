# app/speech-macos: Lookout's on-device speech library

A Swift static library with a C ABI that the Tauri shell links in process (PRD §6.1, §6.5). It
runs engine A1 of PRD §7.1, SpeechAnalyzer with `DictationTranscriber`, **on device only**
(D-015). While the key is held it sends partial transcripts, so the core can prefetch (§7.2),
and the stable (finalized) part of each, so the core can act on a stable partial (§7.1 lever 2).
After key-up it sends exactly one final transcript. It holds audio in RAM for the query only and
never writes it anywhere (§15.1, §15.2).

- **ABI:** [`include/lookout_speech.h`](include/lookout_speech.h), which is the contract: events,
  threading, lifetimes and codes.
- **Products:** `liblookout_speech.a`, and `lookout-transcribe` (WAV in, JSON lines out).
- **Status (20 Nov 2026):** version 0.2.0, built and unit-tested, 103 tests passing. **It has
  never run against the real recognizer.** That needs the owner (see "What needs the owner"),
  because calling the recognizer can show a permission prompt and there are no voice fixtures
  yet.

## Build and test

Build output stays outside the iCloud-synced repo (CLAUDE.md), under
`$LOOKOUT_DATA_DIR/cache/speech-macos` (default `~/lookout-data`):

```sh
cd app/speech-macos
scripts/build.sh            # everything below, in this order
# or step by step:
swift test --scratch-path "${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
swift build -c release --scratch-path "${LOOKOUT_DATA_DIR:-$HOME/lookout-data}/cache/speech-macos"
codesign --sign - --force --identifier dev.lookout.transcribe <scratch>/release/lookout-transcribe
scripts/check-symbols.sh           # deny list + the reviewed import baseline (see "Static checks")
scripts/check-symbols-selftest.sh  # the check above must reject 20 crafted archives
scripts/link-check.sh              # a C program links the archive with the flags below and runs
```

**Outputs:**
- `<scratch>/release/liblookout_speech.a`: 907,616 bytes at the default scratch path; 9 Swift
  objects plus `version.c.o`. The size depends on the scratch path, because absolute source and
  module-cache paths are embedded; the symbol sets do not.
- `<scratch>/release/lookout-transcribe`, signed ad hoc as `dev.lookout.transcribe` with its
  embedded Info.plist bound (`codesign -dv`: `Identifier=dev.lookout.transcribe`,
  `Info.plist entries=4`), so its signing identity and its bundle identifier agree.

**Toolchain:** Swift 6.2.3 in Swift 6 language mode, with warnings treated as errors. Xcode 26.2,
macOS SDK 26.2 (25C57), deployment target macOS 26.

**What the tests need:**
- No microphone, no permission, no model and no audio fixture.
- The real recognizer is unreachable from the test process. A C initializer in
  `Tests/LookoutSpeechTestBootstrap` runs when the test bundle loads, before any test, and puts
  a trap engine and a fake status probe behind the library's seams for the whole process. A test
  that forgets to install a mock, in any class or in a filtered run, gets an ERROR event, never
  `DictationEngine` or the Speech framework's status queries. `SpeechTestCase` also resets the
  seams around every test. `ProcessTrapTests` checks the initializer ran.
- Tones are generated in memory; nothing is written to disk.

## Linking it from the shell (Rust)

The M1 spike's [`tauri-app/build.rs`](../../spikes/m1-platform/tauri-app/build.rs) does this and is
the reference. The minimal link line, checked by `scripts/link-check.sh`:

```
-L <dir of liblookout_speech.a> -llookout_speech
-L $(xcrun --sdk macosx --show-sdk-path)/usr/lib/swift   # Swift runtime .tbd stubs
-Wl,-rpath,/usr/lib/swift                                 # where the OS keeps the Swift runtime
-framework Foundation -framework AVFoundation -framework CoreMedia -framework Speech
```

In `build.rs` terms:
- `cargo:rustc-link-search=native=…/release`, then `cargo:rustc-link-lib=static=lookout_speech`;
- the SDK's `usr/lib/swift` as a search path;
- `cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift`;
- `cargo:rustc-link-lib=framework=…` for the four frameworks.

**Autolinking.** The Swift objects carry autolink entries for the Swift runtime
(`libswiftCore`, `libswift_Concurrency`, `libswiftSynchronization`, …) and for the frameworks
their imports declare, so ld adds those itself. No compatibility libraries are needed, because
the deployment target is macOS 26.

**Links checked:**
- a C host, `scripts/link-check.sh`, on every build;
- the spike, `cargo build --features speech`, dev and release, against version 0.1.0 (19-20 Nov).
  Version 0.2.0 was not relinked into the spike, because the spike's build script writes into
  the spike's own folder. Its link surface is unchanged: the same 10 exports and the same 58
  autolink entries, and its imports are the old set minus 3.

**The C ABI.** Ten functions are exported (`nm -gU`, all `external`): the seven the spike
declares, plus the three process-wide setters described below. The archive also exports the
Swift module's `package` symbols; none of them is part of the ABI.

**Info.plist** (the host app's, not this library's):
- `NSSpeechRecognitionUsageDescription`: the spike already has it.
- `NSMicrophoneUsageDescription`: for `cpal`.

### The ABI in brief (the header has the full contract)

| Call | When | Notes |
|---|---|---|
| `lk_speech_status(locale)` | any time, off the audio thread | 0 ready; 1 model not installed; 2 Speech Recognition denied or restricted; 3 locale unsupported; <0 error. Blocks up to 10 s; never prompts or downloads |
| `lk_speech_start(locale, rate, cb, ctx)` | key-down | Returns at once (measured p50 0.7 to 1.0 ms at 48 kHz, mostly allocating and pre-faulting the 4 MB ring so the audio thread never takes a page fault). The recognizer is set up in the background while pushed audio waits in the ring. NULL only for bad arguments |
| `lk_speech_push_f32(s, mono, n)` | audio thread | Copies into a lock-free ring (at least 20 s); no lock, allocation or system call. Any refused push makes the session end in ERROR `-3` |
| `lk_speech_finish(s)` | key-up | Drains, adds the silence pad (default none), finalizes, then exactly one FINAL or ERROR. Does not wait. A 15 s watchdog turns a hang into ERROR `-6` |
| `lk_speech_cancel(s)` | any time | No callback and no audio to the recognizer after it returns; safe from inside a callback. It waits for a running callback and for one audio chunk at most (measured: at most 1.3 to 1.7 ms per run during a 19 s backlog) |
| `lk_speech_free(s)` | when no other call on the session is in flight | Cancels if needed and zeroes the ring. No push, finish or cancel may overlap or follow it |
| `lk_speech_set_contextual_strings`, `_set_silence_pad_ms`, `_set_model_retention` | before `start` | **Added** (see below). Process-wide and copied at `start`. The library reads no environment variables and no files |

**Events:**
- **READY** comes first. Its `audio_end_s` is the audio buffered during setup, so setup time is
  measurable.
- **PARTIAL** carries the whole hypothesis so far: finalized segments plus the tentative tail,
  which is what `Parser::prefetch_hint` takes. It is sent whenever that text changes (byte for
  byte), including after key-up.
- **STABLE** (0.2.0) carries the stable part of the hypothesis: the finalized text, which the
  recognizer never revises, with its own audio range. It is sent whenever it changes, right after
  the PARTIAL for the same result, or alone when a tentative result was finalized unchanged.
  Its text is always a UTF-8 byte prefix of the PARTIAL text current when it is sent (by
  construction: both come from one ordered list, under one lock), so the latest PARTIAL and the
  latest STABLE together are PRD §7.1's `Partial { text, stability, t_audio_ms }`. Because the
  recognizer never revises finalized results and reports them in time order, it is also a
  prefix of every later PARTIAL and of the FINAL.
- **FINAL** carries only the finalized segments.
- **Audio times** are seconds since the session's first sample.
- **Callbacks** run on a serial queue that the session owns, never on the caller's thread.
- **Unknown kinds:** later versions may add more; a consumer ignores kinds it does not know
  (the spike passes `kind` as a `u32` and maps 4 to `Unknown`).

### Additions to the ABI the lead specified (all additive; the spike is unaffected)
- **`lk_speech_set_contextual_strings(strings, n)`.** PRD §7.1 says viewpoint aliases go in as
  contextual strings. That is the reason A1 is `DictationTranscriber`, and the specified ABI had
  no way to pass them. They go to `AnalysisContext.contextualStrings[.general]`. NULL, empty,
  blank and non-UTF-8 strings are rejected.
- **`lk_speech_set_silence_pad_ms(ms)`.** PRD §7.1 finalization lever 1, "measured, not assumed",
  so it defaults to 0: a caller that never sets it runs with lever 1 off (the spike does not set
  it). TODO(M1): the lead records the default in DECISIONS, and the shell and harness set it
  explicitly and log it with each trial.
- **`lk_speech_set_model_retention(mode)`.** `SpeechAnalyzer.Options.ModelRetention`: while in
  use, lingering (the default) or process lifetime. Idle latency is the headline (§10.6), and this
  setting affects it, so the bake-off should be able to vary it.
- **`LK_EV_STABLE = 4`** (0.2.0): the stable part of the hypothesis (above). PRD §7.1's `Partial`
  has a `stability` field, and lever 2 ("Tier 0 painted from a stable partial", allowed only while
  the wrong-paint rate stays ≤ 2 %) needs to know what the recognizer has settled.
- **Named codes.** `LK_SPEECH_*` status and error codes, and `LK_SPEECH_RETAIN_*`.

## What needs the owner

1. **The on-device model.**
   - **Check:** `lookout-transcribe status --locale en-US` prints `lk_speech_status`; code 1
     means the model is missing. It also prints a `reservations` line:
     `AssetInventory.reservedLocales` for this tool's identity, and `maximumReservedLocales`.
   - **Install:** `lookout-transcribe install-assets --locale en-US --yes`. This downloads
     Apple's dictation model from Apple's servers (`AssetInventory`). It is the only code in the
     package that can make the system fetch anything, it lives in the CLI, not the library, and
     it needs `--yes`. It prints a `reservations` line before and after.
   - **The library never downloads.** A missing model is ERROR 1, not a fallback.
   - **Reservations.** Apple reserves models per app. A model installed through the CLI
     (`dev.lookout.transcribe`) may still need a reservation for the spike
     (`dev.lookout.spike-m1`). If SpeechAnalyzer reports `assetLocaleNotAllocated`, the library
     calls `AssetInventory.reserve(locale:)` once and retries; this downloads nothing. If the
     reservation fails, the session ends with ERROR `-8`.
   - TODO(M1): **which identity reservations (and TCC) key on is unverified.** It is either the
     embedded Info.plist's bundle identifier or the code-signing identifier. For the CLI the two
     now agree (`scripts/build.sh` signs it as `dev.lookout.transcribe`). The spike's cargo
     binaries are linker-signed ad hoc with identifiers like `m1_spike-e8e55116026759cb`, which
     change with the build hash, and their Info.plist is not bound. If the key is the signing
     identifier, every spike rebuild is a new app, and repeated reservations could reach
     `maximumReservedLocales` (then ERROR `-8`). The `reservations` lines before and after the
     first run are the evidence.
2. **Speech Recognition permission: probably not needed.**
   - Apple: the authorization "only applies to speech recognition using SFSpeechRecognizer.
     SpeechAnalyzer transcriber modules don't send audio data of the user's voice to Apple's
     servers."
   - The library reads `SFSpeechRecognizer.authorizationStatus()`, which does not prompt, and
     never calls `requestAuthorization`; `scripts/check-symbols.sh` checks for that, as a symbol
     and as a selector.
   - **The library's policy:**
     - "not determined": sessions run.
     - "denied" or "restricted": status and ERROR code 2. The user's no is honoured, even though
       the on-device module would not need the permission.
   - TODO(M1): confirm on this Mac that a session runs with "not determined" and shows no prompt.
     One outside report of a macOS test says it does.
3. **The microphone** belongs to the shell (`cpal`), not to this library. The library never opens
   it.
4. **Running anything on speech.**
   - Voice fixtures come only from the owner, under `$LOOKOUT_DATA_DIR/fixtures/voice/`.
   - `lookout-transcribe FILE.wav` replays one at the audio's own pace through the C ABI.
     `--fast` pushes at up to 20 times that pace and waits for READY once 15 s are buffered
     (nothing drains the 20 s ring before the recognizer is ready, so unbounded pushing would end
     a long file in ERROR `-3`). `--no-text` keeps only lengths.
   - It prints `meta`, one `event` per callback (`host_ns` on CLOCK_UPTIME_RAW, `wall_s`,
     `audio_pushed_s`, `audio_start_s`/`audio_end_s`) and a `summary` (`finish_to_terminal_ms`,
     push timing).
   - **Its timings are diagnostics**, and `meta.timings` says so: there is no microphone, `cpal`
     or device stage, so they are never D-004's release-to-final measure or the PRD §10.6
     replay (CLAUDE.md: no stage is mocked in a measured run). That is the M1 harness's job.
   - Exit status: 0 FINAL, 1 ERROR, 2 usage, 3 unreadable WAV, 4 not ready.

## How the M1 spike's checks 6 and 7 use it

`spikes/m1-platform/tauri-app/src/speech.rs` and `checks.rs`, and the spike README, check 6.

**Build.** `cargo build --release -p m1-spike-app --features speech` (with
`CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target`). `build.rs`:
- finds `liblookout_speech.a` in `$LOOKOUT_DATA_DIR/cache/speech-macos/release`;
- checks that the header declares the seven functions;
- links as above.

**Before the trials.** The spike calls `lk_speech_status(locale)`. It refuses to run on a
non-zero code and prints its meaning.

**Each trial:**
- **Key-down:** `lk_speech_start(locale, device rate)`, then the cpal stream pushes 4,096-frame
  (or smaller) mono chunks with `lk_speech_push_f32`. The spike times every push against the
  ~1 ms budget.
- **Key-up:**
  - the stream is dropped, so no push follows;
  - `lk_speech_finish`;
  - a wait of up to 10 s for FINAL;
  - `lk_speech_cancel`, then `lk_speech_free`.
- **The library guarantees what the pass rule needs:**
  - one FINAL, after `lk_speech_finish`, and never before it;
  - PARTIALs with text while the key is held;
  - no ERROR on the happy path.
- **STABLE events** reach the spike as kind 4, which it records as `Unknown` and its verdicts
  ignore. The spike sets no silence pad, so its trials run with the default 0 ms.
- **What the trial measures:** `key_up_to_finish_ms` and `finish_to_final_ms` split D-004's
  release-to-final measure. READY's arrival shows the setup time.

**Check 7** watches the network while check 6 runs. Self and the speech-recognition processes
must move zero bytes. The model runs outside the app's process ("It operates outside of your
application's memory space", WWDC25 session 277), so the monitor's daemon groups matter.

## Research record (20 Nov 2026; from the installed SDK, not from memory)

**Installed toolchain:**
- Xcode 26.2 (17C52), macOS SDK 26.2 (25C57), Swift 6.2.3; macOS 26.5.1 (25F80), arm64.
- Speech module: `Speech.framework/…/arm64e-apple-macos.swiftinterface`,
  `-user-module-version 3510.3.1`.

**APIs this library uses** (all macOS 26.0):

| API | Use |
|---|---|
| `SpeechAnalyzer` (actor) | `init(modules:options:)`, `setContext(_:)`, `prepareToAnalyze(in:)`, `start(inputSequence:)`, `finalizeAndFinishThroughEndOfInput()`, `cancelAndFinishNow()`, `static bestAvailableAudioFormat(compatibleWith:considering:)` |
| `SpeechAnalyzer.Options` | `init(priority:modelRetention:)`; `ModelRetention`: `.whileInUse`, `.lingering`, `.processLifetime` |
| `DictationTranscriber` | `init(locale:contentHints:transcriptionOptions:reportingOptions:attributeOptions:)`, `Preset.progressiveShortDictation` ("immediate transcription of about a minute of live audio"), `ReportingOption.volatileResults`, `static supportedLocale(equivalentTo:)`, `results` (`Result`: `range`, `resultsFinalizationTime`, `text`, `isFinal`) |
| `AnalyzerInput` | `init(buffer:)`; the buffer must already be in the analyzer's format |
| `AnalysisContext` | `contextualStrings[.general]` |
| `AssetInventory` | `status(forModules:)` (`.unsupported`, `.supported`, `.downloading`, `.installed`), `reserve(locale:)`; and, in the CLI only, `reservedLocales`, `maximumReservedLocales`, `assetInstallationRequest(supporting:)` then `AssetInstallationRequest.downloadAndInstall()` |
| `SFSpeechRecognizer` | `authorizationStatus()` only |
| `SFSpeechError.Code` | mapped to ABI codes: `.noModel`, `.assetLocaleNotAllocated`, `.tooManyAssetLocalesAllocated`, `.cannotAllocateUnsupportedLocale`, `.insufficientResources`, `.audioDisordered`, `.unexpectedAudioFormat`, `.incompatibleAudioFormats`, `.moduleOutputFailed`, `.timeout` |
| `AVAudioConverter` | `init(from:to:)`, `convert(to:error:withInputFrom:)`. Its header: input data "must not be cleared or re-filled until the input block is called again or the conversion has finished" |

**Names that differ from older material:**
- The WWDC25 talk's `AssetInventory.allocatedLocales` / `deallocate(locale:)` are
  `reservedLocales`, `reserve(locale:)` and `release(reservedLocale:)` in the shipping SDK
  (`maximumReservedLocales` caps them).
- Apple's current documentation lists `AnalyzerInputConverter`, `AssetInputSequenceProvider`,
  `CaptureInputSequenceProvider` and `Options.ignoresResourceLimits`. They are **macOS 27.0+**
  and absent from SDK 26.2. So the conversion here is `AVAudioConverter`: the analyzer does not
  resample.

**Result semantics** (`SpeechModuleResult.isFinal`):
- `isFinal` equals `resultsFinalizationTime >= range.end`.
- A volatile result "may or may not" be replaced, and "there is no guarantee that this result
  will be reissued" as final.
- So `TranscriptAssembler` replaces volatile text by overlapping audio range. It drops volatile
  text that ends before the finalization time, and FINAL is the final segments only. STABLE is
  the final segments that come before the first volatile one in the hypothesis's time order;
  under these rules that is every final segment.

**On-device statements:**
- `DictationTranscriber` "uses the same speech-to-text machine learning models as system
  dictation features do, or as SFSpeechRecognizer does when it is configured for on-device
  operation", and "does not support languages or locales that SFSpeechRecognizer only supports
  via network access".
- WWDC25 session 277: "transcription is entirely on device but the models need to be fetched".
- No Apple page says the analyzer makes no network calls at all. Check 7 is the evidence.

## Inside (for whoever changes it)

- **`CABI.swift`:** the `@_cdecl` entry points. **The compiler does not check them against the
  header's prototypes**: only `lk_speech_cb` and the constants come from the header (the
  `LookoutSpeechC` module), so a drifted prototype still builds the library. The test target
  catches drift: `ABITests.testEntryPointsHaveTheHeadersExactTypes` assigns each entry point to
  a variable typed from the header's declaration and fails to compile with a plain type error
  (checked on a copy with a parameter changed to `Int64`). Without it the only symptom is a
  compiler crash in the test build ("SILFunction type mismatch for 'lk_speech_finish'").
  `scripts/build.sh` builds the tests before the release archive. `version.c` holds
  `lk_speech_version` and `_Static_assert`s.
- **`FloatRing`:** SPSC ring; the push side is two atomic loads, at most two `memcpy`s and one
  store. Consumed samples are zeroed.
- **`Session`:**
  - **Push path:** the ring, plus an overflow flag that a refused push sets and nothing clears
    (a counter alone could wrap: two pushes of `Int.max` frames once turned an overflow into a
    FINAL).
  - **Work queue:** a 5 ms timer drains at least 10 ms at a time, converts and feeds. Every
    chunk, including each silence-pad chunk at key-up, first checks for a cancel or a failure.
  - **Callback queue:** the only place callbacks run.
  - **Control mutex:** every event is decided and queued under it. That is what makes "one
    terminal event, nothing after it", "no callback after cancel returns" and "STABLE is always
    a prefix of the latest PARTIAL" hold.
- **`TranscriptAssembler`:** finals and volatiles in time order (ties: arrival order, finals
  first). `current`, `stable` and `finalTranscript` come from one ordered list.
- **`PCMConverter`:** `AVAudioConverter` from Float32 mono at the session rate to
  `bestAvailableAudioFormat`; it handles the silence pad and the resampler flush. It keeps
  converting until the converter asks for more input, then zeroes its Float32 input copy.
- **`DictationEngine`:** the only `RecognizerEngine`. The tests' `MockEngine`
  (`Tests/LookoutSpeechMocks`, in no product) sits behind the same seam; it can replay a scripted
  timeline of results, delay its feeds, and count feeds after a fence.

### Static checks (PRD §15.1)
- **Deny list:** `scripts/forbidden-symbols.txt`, one rule per line, read by both
  `check-symbols.sh` (release archive) and `StaticChecksTests` (debug objects). The rules cover
  networking (Foundation URL loading, CFNetwork, Network.framework, BSD sockets and DNS),
  server-capable recognition, `requestAuthorization`, model download, microphone and recording,
  file writing (Foundation, CoreAudio, POSIX) and file or URL reading. They are matched against
  undefined symbols and against Objective-C selector names: an Objective-C call such as
  `NSData.write(toFile:)` leaves only a selector string and a plain `_objc_msgSend` import. The
  Foundation rules are anchored to their owners (`10Foundation4DataV5write`, not `5write2to`,
  because the archive already imports `Swift.Double.write(to:)` for string interpolation).
- **Allow list:** `scripts/archive-imports.txt`, the reviewed set of everything the release
  archive imports (400 symbols) and every selector name it contains (39). Any addition or
  removal fails until someone reviews it and runs `scripts/check-symbols.sh --update-baseline`,
  which prints what changed and refuses to baseline an archive that fails the deny list.
- **Self-test:** `scripts/check-symbols-selftest.sh` compiles 20 objects that each call one
  forbidden kind of API (Swift, C and Objective-C, including a selector-only call and a name
  hidden in data), adds each to a copy of the archive, and requires check-symbols to fail with
  that category. It also requires a failure for a harmless new import (the baseline alone), and
  for an archive without the C ABI, and a pass for the real archive.

## Measured here (unit level, mock engine; no recognizer, no audio fixture)

| What | Value | How |
|---|---|---|
| `lk_speech_push_f32`, 480 frames | p50 0.21 µs, p99 5.12 µs, p99.9 133.71 µs, max 506.46 µs; 0 of 5,000 over 1 ms | `PushLatencyTests`: host clock around each call, 10× real time, work queue converting concurrently, ordinary (not real-time) thread |
| `lk_speech_start` at 48 kHz | p50 0.818 ms, max 2.546 ms (50 calls) | `StartLatencyTests` |
| Callbacks after `lk_speech_cancel` returned | 0 (867 before it, in 300 sessions) | `CancelTests` stress |
| Feeds to the recognizer after `lk_speech_cancel` returned | 0 in 200 random-timing sessions (171 were feeding, 67 with a 500 ms pad) | `CancelEdgeTests`, the mock's fence |
| `lk_speech_cancel` wait | max 1.550 ms during a 19 s backlog drain (5 trials); max 13.462 ms during a 2 s pad fed to a recognizer taking 10 ms per buffer (3 trials; one buffer plus sleep overshoot, against about 200 ms for the rest of the pad) | `CancelEdgeTests.testCancelWaitsForOneChunkAtMost` |
| Footprint change | -4.2 MB after 150 full sessions, +4.0 MB after 500 more start+free cycles (27.5 → 23.3 → 27.3 MB) | `LeakTests`, `proc_pid_rusage` `ri_phys_footprint`; a leaked ring would add 4 MB each |
| Archive | 907,616 bytes | `stat` |

These are from the final `scripts/build.sh` run of version 0.2.0 on 20 Nov (load average about
2.6, other agents building on the same machine). **Across six runs** of the 0.2.0 library on
20 Nov (load average 2 to 5; between runs only a per-buffer cancel check and comments changed):
- **push p50:** 0.21 to 0.88 µs; **p99:** 2.71 to 5.12 µs; **p99.9:** 6.04 to 133.71 µs;
  **max:** 10.08 to 733.21 µs; **over 1 ms:** 0 of 30,000;
- **start p50:** 0.737 to 0.963 ms; **max:** 0.977 to 2.546 ms;
- **cancel wait, per-run maximum:** 1.259 to 1.726 ms during the backlog; 5.167 to 13.462 ms
  during the pad (one 10 ms buffer, plus sleep overshoot);
- **after cancel returned:** 0 callbacks in 1,800 stress sessions; 0 feeds in 1,200 sessions;
- **footprint change:** -4.2 to +0.1 MB after 150 sessions; -3.5 to +4.0 MB after 500 cycles.

Version 0.1.0's last run before these changes (same machine, 20 Nov) had one push of 11.0 ms
among 5,000 (p99 5.38 µs), inside that test's bound of 0.1 %: the test thread is an ordinary
thread that the scheduler can preempt. The numbers move with machine load. None of them
measures speech latency; that is M1's replay report.

## Not done, or not known yet

- TODO(M1): **first real run (owner).** Run `status`, then `install-assets` if needed, then
  check 6 and check 7 of the spike. Read READY latency, partial and stable lag, and release →
  final (D-004). Keep the `reservations` lines.
- TODO(M1): **assumptions about the real analyzer, untested here:**
  - result ranges start at 0 for the first buffer when `AnalyzerInput` has no `bufferStartTime`;
  - `progressiveShortDictation` sends volatile results within a few hundred milliseconds, and
    finalizes while the key is held (otherwise STABLE arrives only after key-up);
  - per-session setup (locale check, asset status, format, `prepareToAnalyze`) is short. If it
    is not, cache the first three per process;
  - where `assetLocaleNotAllocated` surfaces, whether reserve-and-retry works, and which identity
    reservations key on (see "What needs the owner").
- TODO(M1): the lead records the silence-pad default (0 ms until measured) in DECISIONS; the
  shell and harness set it explicitly and log it per trial.
- TODO(M1): measure the silence pad and the model retention in the bake-off (PRD §7.1 levers).
  Also measure whether a launch-time warm-up (`prepareToAnalyze`, `.processLifetime`) shortens
  key-down → READY.
- TODO(M2): PRD §6.1 also puts the Foundation Models parser fallback in this Swift library (PLAN
  M2 item 3). It is not here.
