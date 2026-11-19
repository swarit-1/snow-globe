# macOS shell and on-device STT — landscape, 18 Nov 2026

Desk research by a research agent from Apple docs and forums, crate docs, GitHub repos and issues; **nothing benchmarked by us yet.** Items marked **unverified** could not be confirmed. Feeds DECISIONS.md D-004 and the M1 platform spike.

**Context:** macOS 27 is now the current macOS release; Apple's docs mix in 27-only APIs (e.g. `CaptureInputSequenceProvider`). This machine runs **26.5.1** with the 26.2 SDK. We target 26.5.x and record the OS/Xcode used for every measurement.

## 1. Tauri and the hotkey

- Tauri stable **2.12.7** (2026-11-18) [releases](https://github.com/tauri-apps/tauri/releases). A 3.0.0-alpha.6 exists (2026-11-06) — avoid.
- `tauri-plugin-global-shortcut` **2.4.1** (2026-10-10), which fixed a deadlock when a handler calls back into the plugin [CHANGELOG](https://github.com/tauri-apps/plugins-workspace/blob/v2/plugins/global-shortcut/CHANGELOG.md).
- Reports both `ShortcutState::Pressed` and `Released` [docs](https://v2.tauri.app/plugin/global-shortcut/); on macOS via Carbon `RegisterEventHotKey` with `kEventHotKeyPressed/Released` [source](https://github.com/tauri-apps/global-hotkey/blob/dev/src/platform_impl/macos/mod.rs).
- No key-repeat while held on macOS [#59](https://github.com/tauri-apps/global-hotkey/issues/59) — suits hold-to-talk.
- Missed releases: no reports found (**unverified**). Test explicitly, especially when the modifier is released before the key.
- macOS 15+ rejects shortcuts whose only modifiers are Option or Option+Shift [forum 763878](https://developer.apple.com/forums/thread/763878).
- **Modifier-only keys (Fn/Globe) are not supported** by the plugin ([#111](https://github.com/tauri-apps/global-hotkey/issues/111), open). They need a `CGEventTap` on `flagsChanged`:
  - A listen-only tap needs **Input Monitoring**; an `NSEvent` global monitor needs **Accessibility** [forum 707680](https://developer.apple.com/forums/thread/707680).
  - An active (swallowing) tap needs Accessibility, and revoking it while the tap runs can hang system input [forum 844416](https://developer.apple.com/forums/thread/844416) (2026-09).
  - `NSEvent` global monitor crashes reported on macOS 26 [forum 819525](https://developer.apple.com/forums/thread/819525) (2026-03).
  - `handy-keys` 0.3.5 (2026-11-14) supports modifier-only and Fn hotkeys and needs Accessibility [docs](https://docs.rs/crate/handy-keys/0.3.0). Handy notes Fn/Globe works only on Apple keyboards [README](https://github.com/cjpais/Handy).

## 2. Non-activating overlay

- `tauri-nspanel` **2.1.1** (crates.io 2026-10-08; repo pushed 2026-10-25; 2 open issues) [repo](https://github.com/ahkohd/tauri-nspanel). Used by Handy, Cap, screenpipe.
- The 2.1.1 source has panel levels Floating…ScreenSaver, `can_join_all_spaces`, `full_screen_auxiliary`, non-activating style, `transparent()`, `order_front_regardless`. Over fullscreen apps on 26.5: **unverified**.
- **Gotcha:** the non-activating behaviour relies on the **private** `_setPreventsActivation:` (panel.rs).
- `macOSPrivateApi` is a no-op in Tauri 2.12.1+ (always on) [config](https://v2.tauri.app/reference/config/).
- **Hidden webviews are suspended by default:** WebKit's `inactiveSchedulingPolicy` defaults to suspend; Tauri's `backgroundThrottling` maps to it (macOS 14+) [Apple](https://developer.apple.com/documentation/webkit/wkpreferences/inactiveschedulingpolicy-swift.property). Have the page signal readiness from JS before revealing [forum 757450](https://developer.apple.com/forums/thread/757450). No first-paint measurements found.
- Alternative: our own NSPanel via `objc2-app-kit`.

## 3. Audio capture

- `cpal` **0.18.2** (2026-08-16); CoreAudio backend needs macOS 14.2+ [README](https://docs.rs/crate/cpal/0.18.2). No time-to-first-samples numbers found.
- An `AVAudioEngine` tap that never fired with a Bluetooth mic has been reported [forum 819525](https://developer.apple.com/forums/thread/819525).
- `NSMicrophoneUsageDescription` in `src-tauri/Info.plist` is merged into the bundle [docs](https://v2.tauri.app/distribute/macos-application-bundle/). Signed hardened-runtime builds also need `com.apple.security.device.audio-input`; without it there is no prompt and no audio [#9928](https://github.com/tauri-apps/tauri/issues/9928).
- `tauri dev`: tauri-codegen 2.7.6 embeds Info.plist into dev binaries, but TCC credits the permission to Terminal when run unbundled. Ad-hoc signing resets grants on every rebuild → use a stable development certificate and launch with `open` [forum 819525](https://developer.apple.com/forums/thread/819525).

## 4. Apple SpeechAnalyzer (macOS 26)

- **API:** `SpeechAnalyzer` takes modules + an async stream of `AnalyzerInput`. Controls include `finalize(through:)`, `prepareToAnalyze` (preheat), model retention and `setContext(AnalysisContext)` [docs](https://developer.apple.com/documentation/speech/speechanalyzer).
- **`SpeechTranscriber`** reporting options: `volatileResults`, `fastResults`, `alternativeTranscriptions` [docs](https://developer.apple.com/documentation/speech/speechtranscriber/reportingoption).
- **`DictationTranscriber`** uses the system dictation models and supports contextual strings, a custom language model (`SFSpeechLanguageModel`) and content hints [docs](https://developer.apple.com/documentation/speech/dictationtranscriber).
- **Vocabulary:** Apple staff say contextual strings currently help only `DictationTranscriber`; `SpeechTranscriber` ignores them [forum 811083](https://developer.apple.com/forums/thread/811083) (2026-01).
- **Assets:** `AssetInventory`; one report of ~20 s to install a locale [marshal #432](https://github.com/reznichenkoandrey/marshal/issues/432).
- **Latency reports (conflicting):**
  - One dictation app, p50: first words 1,697 ms (`SpeechTranscriber`) vs 1,284 ms (Dictation); **stop → final text 702 ms vs 602 ms** [justspeaktoit #614](https://github.com/crmitchelmore/justspeaktoit/issues/614) (2026-11).
  - Same project, offline: first partial after ~1.1 s of audio (`SpeechTranscriber`) vs ~0.56 s (Dictation). Cold `prepareToAnalyze` ~110 ms, 2–20 ms with the model kept loaded [PR #1385](https://github.com/crmitchelmore/justspeaktoit/pull/1385).
  - Vendor benchmark (M5 Max, 26.5): 125–132 ms median end-of-audio → final [dev.to](https://dev.to/iravoice/apple-speechanalyzer-vs-whispercpp-a-40-speaker-mac-benchmark-40i4) (Aug 2026).
  - The 5× spread between these is exactly why we measure on our own replay set.
- **Breakage:** on 26.3, live streaming input (`start(inputSequence:)`) failed with `nilError` while file input worked (FB22149971) [forum 818005](https://developer.apple.com/forums/thread/818005). No confirmed 26.4/26.5 regressions found.
- **Outside an app bundle:** works; the `yap` CLI does live dictation with permissions granted to the terminal [yap](https://github.com/finnvoor/yap).
- **From Rust:** the `speech` crate 0.9.3 handles files only [repo](https://github.com/doom-fish/speech-rs); `swift-rs` 1.0.9 has no async/closure support; `swift-scribe-rs` uses a separate Swift helper process [repo](https://github.com/NimbleAINinja/swift-scribe-rs). **Recommended:** an in-process Swift static library exposing a plain C ABI (`@_cdecl`), the pattern `fluidaudio-rs` uses.

## 5. Other on-device STT

| Engine | Streaming | Latency / accuracy | Licence | Integration | Vocabulary |
|---|---|---|---|---|---|
| **FluidAudio Parakeet** v0.18.3 (2026-11-07) | EOU streaming model (320 ms chunks, 4.87 % WER); TDT v2 for batch | TDT v2: 2.1 % WER, ~146× real time on M4 Pro (Neural Engine) [benchmarks](https://docs.fluidinference.com/reference/benchmarks) | code Apache-2.0; weights CC-BY-4.0 | `fluidaudio-rs` 0.14.8 (MIT); first run downloads ~500 MB | boosting, benchmarked |
| **whisper.cpp** v1.9.5 (2026-10-06) | sliding-window re-decode only | small.en 4.28 % WER, ~122–125 ms median end-of-audio → final (vendor benchmark above) | MIT | `whisper-rs` 0.16 | `--prompt` |
| WhisperKit (argmax-oss-swift v1.1.1) | mic streaming via CLI | 0.46 s on M3 Max [paper](https://arxiv.org/html/2507.10860v1) | MIT | Swift | Pro-only [repo](https://github.com/argmaxinc/WhisperKit) |
| Moonshine v0.1.6 | yes | vendor claims, unverified | MIT | community `moonshine-rs` | not documented |
| Kyutai STT | yes | 0.5 s delay (1B), 2.5 s (2.6B) | CC-BY-4.0 | MLX | experimental; repo idle since 2026-01 |
| NVIDIA Nemotron streaming 0.6B | 80–1,120 ms chunks | — | "other" licence [HF](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) | — | — |

## 6. Foundation Models as the LLM fallback

- `@Generable` guarantees output matching a Swift type, so structured JSON parsing works [docs](https://developer.apple.com/documentation/foundationmodels).
- 4,096-token context per session [docs](https://developer.apple.com/documentation/foundationmodels/managing-the-context-window).
- Unavailable when Apple Intelligence is off, the device is ineligible, or the model isn't downloaded [docs](https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel/availability-swift.enum/unavailablereason). Device and Siri languages must match; not in mainland China [Apple](https://support.apple.com/en-us/121115).
- **No Mac latency measurements found.** Apple's 2024 figures: ~0.6 ms per prompt token before the first token, 30 tok/s on iPhone 15 Pro [Apple ML](https://machinelearning.apple.com/research/introducing-apple-foundation-models).
- Rate limits are undocumented. CLI tools without UI do get rate-limited; foreground UI apps reportedly don't [DTS 787737](https://developer.apple.com/forums/thread/787737). So it must run in the app process, not a headless helper.

## 7. WKWebView graphics

- WebGL2 since Safari 15, on Metal [WebKit](https://webkit.org/blog/11989/new-webkit-features-in-safari-15/). macOS 26 removed WKWebView's 60 fps cap [note](https://docs.rs/crate/tauri-plugin-macos-fps/0.1.0). One report of jitter in a Tauri WKWebView that Safari didn't show [Babylon forum](https://forum.babylonjs.com/t/performance-between-safari-and-wkwebview-tauri/60811).
- WebGPU on by default in macOS 26 [gpuweb](https://github.com/gpuweb/gpuweb/wiki/Implementation-Status); very likely in Tauri WKWebView on 26.5.1 but **unverified** — check `navigator.gpu` at runtime.
- WebGL in OffscreenCanvas since Safari 17 (unofficial bug comments) [WebKit 183720](https://bugs.webkit.org/show_bug.cgi?id=183720). No `preserveDrawingBuffer` issues found.

## Shortlist (agent's) and platform risks

**On-device STT to benchmark:** (1) SpeechAnalyzer, both `DictationTranscriber` (contextual strings) and `SpeechTranscriber`; (2) FluidAudio Parakeet TDT v2 transcribing the whole clip on release, with the EOU model for partials; (3) whisper.cpp small.en with `--prompt` as a mature baseline.

**Top platform risks:**
1. Modifier-only hotkeys need a `CGEventTap`, with Input Monitoring/Accessibility grants that reset on every rebuild during development, and a swallowing tap can hang input if the permission is revoked.
2. SpeechAnalyzer is immature: live input broke on 26.3, `SpeechTranscriber` ignores vocabulary, first words take 1.1–1.7 s in one app, there is no Rust binding for live input, and behaviour may drift as macOS 27 ships.
3. The overlay relies on a private API, hidden webviews are suspended by default (first paint after release unmeasured), and Foundation Models' rate limits rule out a headless helper.
