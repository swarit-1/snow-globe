# M1 1a platform spike

Renamed for Snow Globe on 21 Nov 2026 (D-080): the bundle identifiers are `dev.snowglobe.spike-m1` and `dev.snowglobe.spike-m1.sck` (were `dev.lookout.*`), the panel is titled "Snow Globe M1 spike panel", and the speech library is linked as `libsnow_globe_speech.a` through `snow_globe_speech.h` (`sg_speech_*`). A new bundle identifier is a new identity to macOS, so permissions granted to the old ones (microphone, Speech Recognition, Accessibility, Screen Recording) must be granted again. The data dir is the workers' (`$SNOW_GLOBE_DATA_DIR`, else `$LOOKOUT_DATA_DIR`, else `~/snow-globe-data` or, until it is moved, `~/lookout-data`).

A throwaway Tauri 2 app and three small helpers, one runnable check for each item in
[PLAN.md](../../PLAN.md) M1 1a (PRD §5.4, §6, §7.1, §10.2–10.6). Nothing here ships; the lead
decides what moves into `app/`.

**PLAN.md M1 1a: stop and report if any check fails.** When a result line says `FAIL`, stop the
spike, run `m1-report show <run-id>`, and send the output and the run file to the lead before
going further. Do not tune anything and rerun until it passes; report the measured values.

Everything was built and unit-tested on 19 Nov 2026 (macOS 26.5.1, Xcode 26.2, Rust 1.98.1).
`m1-spike` was rebuilt with `--features speech` against speech library 0.2.0 at 12:36 UTC on
20 Nov (D-074; check 6). **No check has been run yet.** Running them needs you at the keyboard
and, for some, a permission.

## What the spike records, and what it never records

Each check appends JSON lines to `$SNOW_GLOBE_DATA_DIR/spikes/m1/<run-id>.jsonl` (default
`~/snow-globe-data/spikes/m1/`). The code refuses to write inside the repository. Every line has:

| field | meaning |
|---|---|
| `run_id`, `check`, `proc`, `pid`, `seq` | which run, which check, which process (`m1-spike`, `m1-sck`, `m1-inject`, `m1-report`) |
| `t_mono_ns` | nanoseconds since that process opened the log (Rust `Instant`) |
| `t_host_ns` | the host clock, `CLOCK_UPTIME_RAW` (= `mach_absolute_time` in ns), shared by every process, so their lines join |
| `kind` | `meta` (versions, OS, arguments), `event`, `result`, `note` (your observation) |
| `name`, `v` | what happened, and its values |
| `status`, `pass`, `criterion` | on `result` lines: `pass` / `fail` / `incomplete` / `skipped`, and the criterion text it was judged by |

Also recorded, so you know what a run file holds before you send it:
- **the frontmost app** (checks 2 and 3): its pid, bundle id and name in every focus snapshot (Terminal, or the app you test with);
- **the signing identity** (check 11): `Identifier`, `CDHash` and the `Signature` kind of `m1-sck`, whether a developer certificate signed it, and that certificate's kind (such as `Apple Development`). Never the certificate's subject, which carries your name, and never the team id;
- **the terminal app** (`TERM_PROGRAM`) and whether `m1-sck` ran from a terminal;
- **the network** (check 7): the names of the interfaces that had a route (`en0`, `utun3`), and the executable names of the spike and of the speech or background daemons that moved bytes;
- **paths and arguments**, with your home folder written as `~`.

Never recorded:
- **audio:** buffers are reduced to an RMS level and dropped;
- **transcripts:** only lengths and word counts. `--print-text` shows partials in your terminal only;
- **frames:** captured frames are read for the tag and dropped;
- **your keystrokes:** only the test tokens (`abc`, `back`, `y`, `n`, `q`) and the key names M, L, C, the arrows and Esc;
- **names:** no device names, no host name, no account name. The network monitor names only the spike and speech or background daemons, never other apps.

## Build (no permission, no window)

```sh
cd spikes/m1-platform
export CARGO_TARGET_DIR="$HOME/lookout-data/cache/cargo-target"   # the same folder after the switch (a symlink)
cargo build --release --workspace
cargo test --workspace                      # 105 unit tests, no device access
cargo clippy --workspace --all-targets -- -D warnings
BIN="$CARGO_TARGET_DIR/release"             # m1-spike, m1-sck, m1-inject, m1-report
```

`m1-report checks` prints every check's PLAN wording, criterion and permission.

**Permissions go to the app you run from.** Run from Terminal (or iTerm, …), macOS asks on
behalf of that app, and rebuilding the spike doesn't reset the grant. Grant only when a check
needs it; the spike checks access but never requests it itself, except where noted. Check 11
also covers a bundled helper with its own identity.

**Hotkey.** The default is `Control+Shift+Space`; pass `--hotkey` to change it (global-hotkey
syntax). Option-only and Option+Shift shortcuts are refused, because macOS 15+ rejects them
(PRD §5.4).

**In every check,** type `q` and Return in the terminal to stop early. The result is then
`incomplete`.

## The checks

### 1. Hotkey press and release, including when the modifier is released first
- **Permission:** none.
- **Command:** `$BIN/m1-spike hotkey` (at least 20 guided trials, about 2 minutes).
- **What you do:** follow the prompt for each trial.
  - Key-first trials, until 10 have been observed: hold the hotkey about 1 s, let go of Space, then the modifiers.
  - Then modifier-first trials, until 10 have been observed: hold the hotkey, let go of the modifiers, keep Space down about 1 s more, then let go of Space.
  - A trial seen as `simultaneous` is asked for again. The run stops after 30 trials whatever was seen.
- **What it measures:**
  - every Pressed and Released call of the global-shortcut handler, with host time;
  - at each release, whether the hotkey's modifiers still read as held (`NSEvent.modifierFlags`, read first thing in the handler);
  - while the key is held, a 2 ms poller of `NSEvent.modifierFlags`, which records when the modifiers went up.
- **How a release is classed** (`observed`, `trigger` and `basis` per trial):
  - modifiers still held at the handler: `key_first`, fired by Space going up;
  - modifiers up 20 ms or more before the release: `modifier_first`, fired by Space going up later;
  - modifiers up within 20 ms of the release, or just before it: in a modifier-first trial, `modifier_first` fired by the modifier going up (`basis: requested_order`), because you were keeping Space down; otherwise `simultaneous`.
- **Pass:** every press yields exactly one Pressed and one Released, with no press while the previous one is unreleased and no release without a press. At least 10 trials must be observed key-first and 10 modifier-first.
- **Look for:** `RESULT hotkey: Pass`, then `verdict.trigger_*`: does a modifier-first release fire when the modifier goes up or when Space does? That changes "release to ask" UX, so report it either way.
  - `modifier_first_release_after_modifiers_ms` is about 0 ms when the release fires on the modifier, and about your extra hold (about 1000 ms) when it fires on Space. A mix of the two means some trials let go of both at once.
  - `trigger_modifier_up_inferred` counts triggers taken from the instructed order, not measured.
- **Option:** `--key-state` also polls Space with `CGEventSourceKeyState`, which measures the trigger directly (`basis: key_state`). That may need Input Monitoring, so it is off by default.

### 2. A non-activating panel over another app that keeps focus
- **Permission:** none.
- **Command:** `$BIN/m1-spike panel` (3 trials).
- **What you do:**
  1. Click into the terminal running the check.
  2. Hold the hotkey about 1 s and let go. The panel appears top right and stays up 4 s.
  3. While it is up, type `abc` and press Return.
  - To test another app instead (TextEdit, or one in full screen), type there, then answer `y` or `n` in the terminal when asked.
- **What it measures:** before the reveal, and at +0, +50, +250 and +1000 ms after it:
  - the frontmost app (NSWorkspace), from the panel's point of view;
  - whether the spike app is active;
  - whether the panel is visible, key and on screen (occlusion state);
  - any key-window event the panel receives;
  - whether `abc` reached the terminal while the panel was up.
- **Pass:** in every trial:
  - the frontmost app never changes and is never the spike;
  - the spike app is never active, and the panel never becomes key;
  - the panel is on screen at +250 ms;
  - the typing reached the other app.
- **Look for:** `RESULT panel: Pass`. The panel sits at the `status` window level. Try `--level floating` if a full-screen app hides it, and report which level worked.

### 3. Click-to-key focus, with focus returning to the previous text field
- **Permission:** none.
- **Command:** `$BIN/m1-spike focus` (3 trials).
- **What you do:** in each trial:
  1. Click into the terminal.
  2. Press and release the hotkey. The panel stays up.
  3. Click the panel.
  4. Press M, then the Left arrow.
  5. Press Esc.
  6. Without clicking anything, type `back` and press Return.
  - With another app, type into its field at step 6, then answer `y` or `n` here after 30 s.
- **What it measures:**
  - when the panel becomes key (Tauri `Focused`), the frontmost app and whether the spike app is active;
  - the keys the page receives while it is key;
  - after Esc: the panel hidden and not key, and the frontmost app unchanged;
  - whether `back` reached the terminal after Esc.
- **Pass:** in every trial:
  - the click makes the panel key, while the spike app stays inactive and the other app stays frontmost;
  - the page receives M and Left;
  - Esc hides the panel;
  - the next typing reaches the previous text field.
- **Look for:** `RESULT focus: Pass`. This is the PRD §5.4 Spotlight-style behaviour. Esc is a global shortcut only while the panel is visible.

### 4. The panel revealed on key-down, with wake-from-suspension timed
- **Permission:** none.
- **Command:** `$BIN/m1-spike reveal`.
  - 5 trials after ≥ 30 s hidden, then 2 after ≥ 360 s hidden, about 20 minutes in all.
  - To compare webview policies, repeat the run with `--throttling throttle` and with `--throttling disabled`. The default is WebKit's suspend.
- **What you do:** wait until the terminal prints `ready`. Then hold the hotkey about 1.5 s and let go.
- **What it measures:**
  - The key-down handler shows the panel (`orderFrontRegardless`), then asks the page to draw. The page reports its next animation frame.
  - **Wake time** is key-down to that report reaching Rust. It is an upper bound on page wake-up, and a lower bound on paint, because composited paint is later (check 8).
  - The page's 50 ms heartbeat gap tells whether it was suspended (gap ≈ the time hidden), throttled (about 1 s) or running.
  - Page reloads are counted. WebKit may unload a view hidden for about 5 minutes.
- **Pass:** in every trial, the wake report arrives before key-up (PRD §5.2: the panel is revealed on key-down, so wake-up happens during speech).
- **Look for:** `RESULT reveal: Pass`, and `by_idle_class` gives wake_ms p50/p95/max and beat gaps for each class. These numbers go in DECISIONS.md.
- **Optional and untried, paint after wake:** run `m1-sck tag --run-id <same id> --seconds 900` in a second terminal after the first trial. Then `m1-report show <id> --write` adds key-down → first composited frame with the reveal's tag (`sck-tag-join`).
  - ScreenCaptureKit may refuse to start on a panel that is hidden at that moment. If so, skip this step; check 8 measures paint on a visible panel.
  - In this combined run, ignore the `sck-tag` and `clock` verdicts that `m1-sck` writes: hidden periods leave long gaps between frames.

### 5. `cpal` capture opened on key-down
- **Permission:** Microphone. macOS asks the first time.
  - If you only grant it during trial 1, that trial records all-zero samples and fails. Grant it, then rerun.
- **Command:** `$BIN/m1-spike cpal` (5 trials).
- **What you do:** hold the hotkey, say a few words, let go.
- **What it measures:**
  - On key-down, the audio thread opens the default input with cpal 0.18.2 (f32, the device's default rate and buffer size). On key-up, it closes it.
  - Key-down → first buffer, and the buffer cadence (callback intervals, frames per callback).
  - Capture timestamp → callback.
  - RMS level, peak and the count of non-zero samples, then the samples are dropped.
  - Stream errors.
- **Pass:** in every trial:
  - the input opens in response to key-down;
  - the first buffer arrives before key-up;
  - the samples carry signal (some are non-zero);
  - there are no stream errors;
  - the stream closes on key-up.
  - The PLAN sets no latency threshold, so time to first buffer is reported, not judged.
- **Look for:** `RESULT cpal: Pass`, `first_buffer_ms`, and the per-trial `interval_ms`.

### 6. The Swift static library (C ABI) returning DictationTranscriber partials
- **Permissions:**
  - Microphone;
  - Speech Recognition, if the library asks;
  - the en-US dictation asset installed (`sg_speech_status` = 1 means it is missing).
- **Build first.** The library comes from `app/speech-macos` (another work package; 0.2.0 is committed in `8443bf3`, D-055). It did not exist when this spike was built, so the check is behind a Cargo feature. The lead built the spike this way at 12:36 UTC on 20 Nov, against `$SNOW_GLOBE_DATA_DIR/cache/speech-macos/release/libsnow_globe_speech.a` 0.2.0 (D-074):
  ```sh
  # after app/speech-macos is built (or set SNOW_GLOBE_SPEECH_LIB_DIR / SNOW_GLOBE_SPEECH_LIB_NAME):
  cargo build --release --offline --locked -p m1-spike-app --features speech
  ```
  - **The Build block above undoes it:** `cargo build --release --workspace` rebuilds `m1-spike` without the feature, and this check then records `skipped`. Before running it, `strings -a "$BIN/m1-spike" | grep 'snow-globe-speech 0.3.0'` must print the library's version line; if it prints nothing, rebuild with the command above. The owner runbook runs that command right after the Build block (step 2.1), and its step 3 has the rest of the sequence, starting with `snow-globe-transcribe status`.
  - `build.rs` looks for `libsnow_globe_speech.a` in `app/speech-macos/.build/{release,arm64-apple-macosx/release,debug}`, `app/speech-macos/build`, then `$SNOW_GLOBE_DATA_DIR/cache/speech-macos`.
  - It checks that `include/snow_globe_speech.h` declares every function.
  - It links the Swift runtime (rpath `/usr/lib/swift`) and the Foundation, AVFoundation, CoreMedia and Speech frameworks.
  - Without the feature, `m1-spike speech` records `skipped` for checks 6 and 7.
- **Command:** `$BIN/m1-spike speech` (5 trials; add `--print-text` to see partials in the terminal).
- **What you do:** wait 3 s for the network monitor. Then hold the hotkey, say a place and a time ("Mount Rainier at sunset tomorrow"), and let go.
- **What it measures:**
  - On key-down: `sg_speech_start(locale, device rate)`, then the cpal stream pushes mono f32 with `sg_speech_push_f32`. Each push is timed against the ABI's ~1 ms budget.
  - On key-up: the stream is dropped (so no push can follow), then `sg_speech_finish`, a wait of up to 10 s for FINAL, then `sg_speech_cancel` and `sg_speech_free`.
  - Every callback's kind and time, the text length and words. No text is logged.
  - Key-down → first partial, and **key-up → FINAL**: D-004's release-to-final measure. It is split at the `sg_speech_finish` call:
    - `key_up_to_finish_ms`: the hand-off to the audio thread plus the CoreAudio stream teardown, which the spike puts before finish;
    - `finish_to_final_ms`: the library's flush and finalize.
- **Pass:** in every trial:
  - `sg_speech_start` succeeds;
  - at least one PARTIAL has non-empty text;
  - exactly one FINAL arrives, and it comes after `sg_speech_finish` was called;
  - no ERROR arrives.
- **Look for:** `RESULT speech: Pass`, `release_to_final_ms`, `key_up_to_finish_ms`, `finish_to_final_ms`, and `push` (max µs, count over 1 ms).

### 7. Network silence during DictationTranscriber, checked with a network monitor
- **Permission:** none. It runs inside check 6's command, unless you pass `--no-netwatch`.
- **Monitor:** macOS's `nettop` in delta mode, 1 s samples:
  `script -q /dev/null /usr/bin/nettop -P -L 0 -d -s 1 -n -x -J time,bytes_in,bytes_out`.
  - `script` gives it a terminal. Measured 19 Nov: piped straight, nettop held all its samples until it exited.
  - nettop cuts process names to 15 characters, and two cuts are ambiguous on this macOS (`com.apple.Speec` is both the speech-recognition broker and the speech-synthesis daemon; `com.apple.siri.` is both the embedded recogniser and other Siri services). So each pid that moved bytes is resolved to its executable name (`proc_pidpath`, no permission).
  - Bytes are grouped as:
    - **self:** the spike, which runs the speech library in its own process;
    - **speech:** the speech-recognition processes found on disk: `localspeechrecognition`, `com.apple.siri.embeddedspeech`, `com.apple.SpeechRecognitionCore.brokerd`;
    - **background:** speech-adjacent and general-purpose daemons, which move data for many clients: `corespeechd`, `speechmaintenanced`, `SpeechDataInstallerd`, speech synthesis, Siri and `assistantd`, `DictationIM`, asset daemons, MobileSoftwareUpdate, `triald`, `nsurlsessiond`;
    - **other:** counted, never named.
  - At the end the app waits up to 8 s for the nettop sample that covers the last trial.
- **Pass:** for every trial, from key-down to 1 s after FINAL:
  - complete nettop samples cover the whole window;
  - zero bytes in or out for **self** and **speech**. Any byte fails.
  - Any byte from a **background** daemon makes the result `incomplete` (inconclusive), never a pass, because it may be unrelated traffic.
- **Report every run,** failed and inconclusive ones included. Do not rerun until it passes.
- **When a run is inconclusive, the Wi-Fi-off run decides.** Turn Wi-Fi off (and unplug any Ethernet), then run check 6 again.
  - The run records `network_at_start.online: false`, and its `netsilence` result is `incomplete` by design.
  - Its `RESULT speech: Pass`, partials and FINAL with no network, shows that the transcriber does not need the network. Send both runs.
- **Look for:** `RESULT netsilence: Pass`. `windows[].bytes.flagged` names every process in the self, speech and background groups that moved bytes, with its executable name.
- **Optional cross-check:** watch a packet-level monitor (Little Snitch, LuLu, or `sudo tcpdump -k -i pktap,all`) during a trial.

### 8. A ScreenCaptureKit window-only capture with an in-surface pixel tag
- **Permission:** Screen Recording ("Screen & System Audio Recording") for the terminal. The first `m1-sck` call makes macOS ask.
- **Commands:** two terminals.
  ```sh
  $BIN/m1-spike tags --run-id m1tags-1 --seconds 120      # shows the panel, new tag every 250 ms
  $BIN/m1-sck tag --run-id m1tags-1 --seconds 30          # start a second or two later
  $BIN/m1-report show m1tags-1 --write                    # adds the cross-process join
  ```
- **What it measures:**
  - The panel draws a 25-cell black/white strip (8 pt cells) at its top-left corner. It encodes (query_gen 12 bits, tier 2 bits) with a sync pattern and CRC-5. Rust computes the cells; the page only paints them.
  - `m1-sck` builds an `SCContentFilter(desktopIndependentWindow:)` for the window titled "Snow Globe M1 spike panel". It captures at the window's native pixel size, BGRA at 60 fps, with no shadow and no cursor.
  - For every frame: status, presentation time, display time, arrival time, the decoded tag, and the geometry ScreenCaptureKit reports (`contentRect`, `contentScale`, `scaleFactor`, `screenRect`). Then the frame is dropped.
  - `m1-report --write` joins each tag request with the first frame carrying it before the next request. That gives request → composited-frame paint times, for requests made while the capture ran.
- **Pass:** both parts must pass.
  - `sck-tag`:
    - the filter is a desktop-independent window filter;
    - ≥ 99 % of complete frames, after the first tagged one, carry a valid tag at the frame's top-left corner. That is the evidence that the frame starts at the panel's corner: in a display capture the panel sits elsewhere in the frame.
  - `sck-tag-join`:
    - no decoded tag is one the app never drew;
    - every tag held ≥ 100 ms during the capture appears in a frame before the next tag.
- **Look for:** `RESULT sck-tag: Pass`, then `join sck-tag-join: PASS`, and `paint_ms` from the join.
  - Also report `content_rect_vs_window` and `content_rect_sizes`. Apple does not document `contentRect`'s unit for a window capture, so this run settles it. `TODO(M1)`: then make the match a criterion, here and in the M1 harness. The frame buffer always has the configured size (ScreenCaptureKit scales into it), so `buffer_size_mismatch` is a sanity count, not evidence.

### 9. `CGEventPost` hotkey injection
- **Permission:** Accessibility for the terminal you run `m1-inject` from (System Settings > Privacy & Security > Accessibility).
  - `m1-inject` exits with `inject-preflight: incomplete` until it is granted.
  - Note for the lead: PRD §1 row 14 lists only Screen Recording and Input Monitoring for the harness. Posting needs Accessibility too.
  - `--tap` also needs Input Monitoring.
- **Commands:** two terminals, same run id and trial count.
  ```sh
  $BIN/m1-spike hotkey --injected --trials 20 --run-id m1inj-1     # wait for "waiting for 20 injected presses"
  $BIN/m1-inject --run-id m1inj-1 --trials 20 [--tap]              # do not touch the keyboard
  $BIN/m1-report show m1inj-1 --write
  ```
- **What it does:**
  - Posts at `kCGHIDEventTap` from a HID-system-state event source.
  - Modifiers go down as flags-changed events, then Space goes down.
  - It holds 600 ms, then releases. Trials alternate: key-first, and modifier-first with a 400 ms wait.
  - Every post is logged with host time before and after.
  - `--tap` adds a listen-only session tap. It records only the hotkey's key codes, with `CGEventGetTimestamp`, arrival host time and mach ticks, which shows the OS event clock (PRD §10.4 t0).
- **Pass:** `inject` (from `m1-report --write`): every posted press and release produces exactly one Pressed and one Released in the app's handler, in order, within 1 s of the trial's last post. A handler call outside every trial (before the first post, between trials, or later) is stray and fails the join.
- **Latency:** post → handler is measured from the host clock read just before each `CGEventPost` call, so it is an upper bound. The post's bracket (after minus before) is reported beside it; a handler that runs inside the bracket is correct, not early.
- **Look for:** `join inject: PASS`, `press_latency_ms`, `release_latency_ms`, `max_post_bracket_ms`, and `release_after` (`key_up` or `mod_up`).

### 10. Host-clock agreement between ScreenCaptureKit timestamps and `Instant`
- **Permission:** Screen Recording. It is the same run as check 8: `m1-sck tag` writes both results.
- **What it measures:**
  - Whether `Instant` is `CLOCK_UPTIME_RAW`. It parses `Instant`'s value and compares it with `CLOCK_UPTIME_RAW` and `CLOCK_MONOTONIC_RAW`, which differ by the time slept since boot.
  - The drift between `Instant` and the host clock across the run, from paired reads before and after the capture.
  - For every complete frame: arrival on `Instant` (mapped to host time) minus the presentation time; display time minus presentation time; and the slope of arrival against presentation time.
- **Pass:**
  - `Instant` reads `CLOCK_UPTIME_RAW`;
  - the drift is no more than the read bracket + 1 µs;
  - no complete frame arrives more than 1 ms before its presentation time, which no single clock allows;
  - the median presentation → arrival lag is 0–250 ms. A different clock would give seconds, or a negative lag;
  - arrival against presentation time has a slope of 1 ± 0.01. The slope is the median over pairs of frames half the run apart, so one stalled frame cannot move it; the least-squares slope is reported beside it.
  - Frames arriving more than 250 ms late are counted as `late_frames` and reported, not failed: one scheduling stall on a busy machine is not a clock mismatch.
- **Already measured (unit test, no permission, 19 Nov 2026; three runs after the fixes):**
  - `Instant`'s value matched `CLOCK_UPTIME_RAW` to within 12.0–83.6 µs, the formatting delay between the two reads (the verdict's tolerance is 1 ms). `CLOCK_MONOTONIC_RAW` was 2,152,000.9 s ahead, which is the time slept since boot, so the identity is conclusive.
  - The drift was +1 to +146 ns over 147–152 ms (200 reads per run, brackets 333–625 ns), inside the bracket + 1 µs criterion. The mach timebase was 125/3.
  - So `Instant` is the host clock on this machine. Only the ScreenCaptureKit half is left to run.
- **Look for:** `RESULT clock: Pass`, `pts_to_arrival_ms`, `late_frames` and `arrival_slope_vs_pts`.

### 11. Whether Screen Recording re-consent prompts appear
- **Permission:** Screen Recording.
- **Command:** with `m1-spike tags --seconds 600` running in another terminal:
  ```sh
  $BIN/m1-sck consent --run-id m1c-<scenario> --scenario <scenario>    # answers y/n at the end
  ```
- **Scenarios:**
  - `first-run`: expected to prompt; informational only;
  - `repeat`: the same binary, minutes later;
  - `after-reboot`;
  - `next-day`;
  - `after-rebuild`: `touch sck/src/main.rs && cargo build --release -p m1-spike-sck`; informational only.
  - The overnight harness runs under `caffeinate -i` (PRD §10.6), from a terminal or as the bundled helper below, so `next-day` on the bundled helper is the scenario closest to it. A launchd scenario was dropped: nothing in the PRD runs the harness from launchd.
- **Harness identity:** to test the helper as its own app, as the overnight harness would be:
  - run `scripts/bundle-sck-helper.sh` (ad-hoc), or pass a stable identity: `scripts/bundle-sck-helper.sh "Apple Development: …"`;
  - run the bundle with the `open -W -n --env … --args consent …` line printed in the script's header;
  - then record what you saw: `m1-report note <run-id> consent prompt_seen=no scenario=repeat`. `prompt_seen` takes yes, no, y, n, true or false, and anything else is refused;
  - and `m1-report show <run-id> --write` redoes the verdict with your note.
- **What it measures:**
  - `CGPreflightScreenCaptureAccess` before and after;
  - how long `SCShareableContent` takes (a waiting prompt shows as a long call);
  - time to the first complete frame;
  - the code-signing identity TCC keys on: Identifier, CDHash and the Signature kind, plus whether a developer certificate signed it and the certificate's kind. The certificate's subject (your name) and the team id are not recorded;
  - your answer.
- **Pass:** an already-approved, unchanged binary on `repeat`, `after-reboot` or `next-day`:
  - shows no prompt;
  - gets shareable content and a first frame within 5 s.
  - `first-run`, `after-rebuild` and `new-identity` runs are recorded but aren't failures by themselves.
- **Look for:** `RESULT consent: Pass` for each repeat scenario. A prompt on a repeat run is exactly the risk in PRD §18.1, so stop and report.

## After the spike

1. Run `m1-report list`, then `m1-report show <id>` for each run, and paste the outputs into the spike notes. The docs agent or the lead copies the numbers into DECISIONS.md (gate evidence: "Spike notes and numbers in DECISIONS.md").
2. Any `FAIL` stops M1 until it is reported (PLAN.md M1 1a).

## Known limits

- **Check 6 links the real library, which has not run yet.** The `--features speech` build links `app/speech-macos`'s static library (0.1.0 at 03:39 UTC on 20 Nov; 0.2.0 since the lead rebuilt it at 12:36 UTC: 1,013 `LookoutSpeech` symbols, Speech.framework; the module's name before the rename, D-080). The header test in `tauri-app/src/speech.rs` compares the library's header with this ABI. `TODO(M1)`: the binary is still signed with the linker's identifier and no bound Info.plist.
- **Checks 2 and 3 confirm "typing reached the other app" automatically only with the terminal as the other app**, because the spike reads its own stdin. With any other app, your `y`/`n` is the evidence, and it is logged as a `note`.
- **The tag is opaque.** The PRD §10.4 paint rule (first frame at ≥ 50 % opacity during a 120 ms cross-fade) belongs to the M1 harness. Here the join reports request → first frame carrying the tag.
- **CRC-5 is not perfect.** It catches every single- and double-cell error. Measured exhaustively in the unit tests (every cut of every pair): a redraw torn between consecutive tags never decodes as a third tag (0 of 29,930 strips in `tags` mode, 0 of 28,392 in `reveal` mode), but between tags 2 to 16 generations apart, 890 of 612,776 torn strips (0.15 %) do. So the join also fails on any tag the app never drew.
- **The stream's size is fixed when the capture starts**, at `contentRect × pointPixelScale` from the filter. A panel dragged mid-run to a display with another scale is scaled into that size, and the tag may stop decoding.
- **The modifier-up trigger is inferred without `--key-state`.** In a modifier-first trial whose release comes within 20 ms of the modifiers going up, the spike takes the release to have fired on the modifier, because you were asked to keep Space down. Letting go of both at once would be misattributed; `modifier_first_release_after_modifiers_ms` shows it.
- **Check 7's speech-recognition list comes from this macOS's disk** (19 Nov 2026). A recogniser under another name would count as `other`, unnamed. The Wi-Fi-off run covers that gap.
- **Permission prompts name the terminal.** When the spike runs from a terminal, macOS asks for and records permissions against that terminal. Only check 11's bundled helper tests an identity of its own.

## Versions (crates.io, checked 19 Nov 2026)

| crate | version | why this one |
|---|---|---|
| `tauri` / `tauri-build` | 2.12.7 / 2.7.6 (18 Nov 2026) | Latest 2.x. 3.0.0-alpha.6 exists; avoided. |
| `tauri-plugin-global-shortcut` | 2.4.1 (10 Oct 2026) | Latest 2.x: Pressed and Released through Carbon `RegisterEventHotKey`. Pins `global-hotkey` ^0.8, resolved to 0.8.0; global-hotkey 0.10.0 exists but the plugin does not use it. 3.0.0-alpha.3 avoided. |
| `tauri-nspanel` | 2.1.1 (8 Oct 2026) | NSPanel subclass with the nonactivating style. It sets the private `_setPreventsActivation:` when available. |
| `cpal` | 0.18.2 (16 Aug 2026) | CoreAudio timestamps are `mach_absolute_time` (host clock). |
| `screencapturekit` | 11.1.2 (2 Nov 2026, doom-fish) | Exposes `SCStreamFrameInfo` (status, displayTime, contentRect, scale) and the pixel-buffer lock. Its Swift bridge is built by its build script. The helper needs `-rpath /usr/lib/swift`, or it aborts at launch (found by the unit tests here). Considered and not used: `objc2-screen-capture-kit` 0.3.2 (raw bindings), or a separate Swift helper (it would duplicate the tag decoder). |
| `objc2` / `objc2-app-kit` / `-foundation` / `-core-graphics` / `-core-foundation` | 0.6.5 / 0.3.2 | The versions Tauri 2.12.7 already uses. |
| `serde`, `serde_json`, `libc`, `chrono`, `tempfile` | 1.0.229, 1.0.154, 0.2.191, 0.4.45, 3.27.0 | |

Resolved underneath: wry 0.57.3, tao 0.37.3, tauri-runtime-wry 2.12.5, coreaudio-rs 0.14.2,
apple-cf 0.11.7. The full graph is in `Cargo.lock` (475 packages).

## Layout

| path | what |
|---|---|
| `common/` | `m1-spike-common`: run log, host clock, statistics, pixel tag, hotkey pairing, accelerator → key code, nettop parser, check list, consent verdict, report joins; `m1-report` |
| `tauri-app/` | `m1-spike`: the Tauri 2 app (checks 1–7, and `tags` for 8, 10 and 11); `ui/` is the panel page; `Info.plist` holds the microphone and speech usage strings, which Tauri's dev-mode build embeds in the binary |
| `sck/` | `m1-sck`: ScreenCaptureKit capture, checks 8, 10 and 11 |
| `inject/` | `m1-inject`: `CGEventPost` injection and the optional listen-only tap, check 9 |
| `scripts/bundle-sck-helper.sh` | wraps `m1-sck` in an app bundle for check 11's identity runs (output under `$SNOW_GLOBE_DATA_DIR/cache/spike-m1/`) |
