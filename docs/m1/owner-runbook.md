# M1 owner runbook: what you do in M1, in order

**Updated:** 2026-11-21, about 01:20 UTC, for the rename to Snow Globe (D-080). Otherwise as of
2026-11-20, about 17:30 UTC.

This is everything M1 (Voice to first pixel, Mon 23 Nov → Mon 7 Dec 2026) needs from you, the
owner, in the order it is needed. Each step says what to run and what to send back. Details live
in the documents it links to; this page only sequences them.

**State on 20 Nov:**
- The 1a spike app is built and unit-tested, and none of its checks has run (D-033).
- The Swift speech library 0.2.0 (`app/speech-macos/`, ticket M1-05) is built, unit-tested and
  committed (`8443bf3`, D-055). It has never run against the real recognizer: step 3 is its first
  real run. Its command-line tool, `lookout-transcribe`, is built at
  `~/lookout-data/cache/speech-macos/release/lookout-transcribe`.
- The lead rebuilt the spike against library 0.2.0 at 12:36 UTC on 20 Nov (D-074). The spike
  README's Build block, which you run first, replaces that build with one without speech, so
  step 2.1 rebuilds `m1-spike` with the speech feature right after it. Spike checks 6 and 7 then
  run on Mon 23 Nov with the others.
- The replay scorer exists since 23 Nov (D-096), but the harness and the recording tool (M1-09)
  are not built yet, so steps 4 and 6 wait for code; each says what it waits for.
- If M0.5 or M1 runs late, the plan puts the freeze-critical work first (PLAN.md, Calendar).
  The speech and harness steps may then wait; the lead will tell you.

**Since 21 Nov: the new names** (D-080). The product is Snow Globe, and this page uses its new
names. The speech tool is `snow-globe-transcribe`, signed as `dev.snowglobe.transcribe`, and the
data folder is `~/snow-globe-data`. The commands below reach it as `~/lookout-data`, the symlink the
switch leaves, so they are right after the switch and never create a second data folder if run
early by mistake (D-080).
- **The switch ran at 23:53 UTC on 22 Nov to 00:03 UTC on 23 Nov** (D-090), after the M0.5 gate
  closed at 23:46:10.754 UTC (D-089). The data folder is now `~/snow-globe-data`.
- **The rebuild is done (01:46 UTC on 23 Nov), so the steps below can start.** The lead confirmed
  the renamed speech tool: `snow-globe-transcribe`, signed `dev.snowglobe.transcribe`, reads
  `snow-globe-speech 0.3.0`, as does its library. The lead then rebuilt the spike offline, the
  Build block and then step 2.1's speech build. Its cargo target folder had been removed on
  22 Nov. Details are in PRD_PROGRESS.md, owner action 6.
- The 20 Nov builds above carry the old names (`lookout-transcribe`, `dev.lookout.transcribe`).
  Don't use them for step 3: permissions and model reservations may follow the identifier
  (D-055).

**Ground rules for every step:**
- Nothing here records audio except step 4, and only while you hold the hotkey.
- A permission goes to the app that asks for it. For the spike that is the terminal you run it
  from (see step 1).
- Report every result, failed and inconclusive ones included. Don't rerun a check to get a pass
  (PLAN.md, M1 1a). Rerun only where the spike README says to: check 5 after a late Microphone
  grant, and check 7 with Wi-Fi off.
- No paid call happens until you have said yes to that vendor's first call (step 5).

## 0. Before Mon 23 Nov

1. **Keep the Mac on AC power** (PRD_PROGRESS.md, owner action 1). Capture and the forecast job
   still run on this laptop (D-030), and every M1 measurement assumes AC power. The same owner
   action asks you to start the lead's supervisor, so that a job that stops is restarted outside
   the lead's session (D-076). The two running jobs stay the session's children until they exit.
2. **Don't upgrade macOS.** It stays on 26.5.x through M4 (PRD §1 row 1); an update mid-milestone
   breaks the latency comparisons.
3. **Free some disk** if you can (owner action 11). `df` showed 27 GiB available at 17:26 UTC on
   20 Nov (19 to 23 GiB earlier that day), and the figure moves as builds and capture run. Apple
   Intelligence's on-device models take several GB (step 1). The recordings are small: 130 takes
   of about 3 s each, at 48 kHz in 32-bit float, come to about 75 MB per input channel. The rate
   and channel count are assumptions until spike check 5 reports them.

**Report back:** nothing, unless the Mac must go on battery for a while; then say when.

## 1. Permissions

Grant each one when the step that needs it asks, not all at once. All the panes are in **System
Settings** (the Apple menu > System Settings…).

| Permission | Exact path | For | When |
|---|---|---|---|
| Microphone | System Settings > Privacy & Security > Microphone | spike check 5; the app; the recording tool; the harness | step 2, when macOS first asks |
| Speech Recognition | System Settings > Privacy & Security > Speech Recognition | probably not needed by the on-device transcriber: the library only reads this setting and never asks (D-055); the app | step 3, only if `snow-globe-transcribe status` reports code 2 |
| Screen Recording | System Settings > Privacy & Security > Screen & System Audio Recording | spike checks 8, 10 and 11; the harness only | step 2 |
| Input Monitoring | System Settings > Privacy & Security > Input Monitoring | spike check 9 with `--tap` and check 1 with `--key-state`; the harness only | step 2, only if you run those options |
| Accessibility | System Settings > Privacy & Security > Accessibility | spike check 9 (`CGEventPost`); the harness's key injection | step 2 |
| Apple Intelligence | System Settings > Apple Intelligence & Siri > Apple Intelligence (switch on) | Foundation Models, the M3 parser fallback (D-005) | any time in M1 |

- **Accessibility is not in PRD §1 row 14.** Injecting the hotkey needs it (D-033, open), so the
  lead will ask you to confirm it.
- **Apple Intelligence downloads its models** after you switch it on. Check free space first
  (step 0). Whether the M1 speech checks also need it isn't known; check 6 will show.
- **Grant to a terminal you keep for the spike.** macOS attributes a permission to the app that
  launched the program. Every program run from that terminal later inherits all five grants:
  Microphone, Speech Recognition, Screen & System Audio Recording, Input Monitoring and
  Accessibility. That includes any coding agent you run in it. With Microphone on, such a
  program could record audio without a prompt.
  - So run the spike from a terminal app you don't use for agents. For example, use Terminal
    for the spike if your agents run elsewhere.
  - Switch Microphone and Speech Recognition off again as soon as checks 5 to 7 are done, and
    turn them back on only when the recording tool or the app asks.
  - Switch the other three off in the same panes when the spike's checks are done.
  - The overnight harness is meant to run as its own bundled app, with its own grants (spike
    check 11 tests exactly that identity).
- **To take a grant back:** switch its row off in the same pane. Removing an app needs the
  minus button there.

**Report back:** which permissions you granted, to which app, and whether any prompt surprised
you.

## 2. The 1a platform spike (Mon 23 Nov, about a day)

Everything is in [spikes/m1-platform/README.md](../../spikes/m1-platform/README.md): build
commands, the exact command per check, what to do with your hands, and the pass criterion,
fixed before any run (D-033). In order:

1. **Build and unit-test** (no permission, no window): the README's "Build" block, with
   `CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target`. Its release build replaces the
   lead's speech build of `m1-spike` with one without speech, so straight after it, in the same
   terminal (still in `spikes/m1-platform`, with the same `CARGO_TARGET_DIR`), rebuild `m1-spike`
   with the speech feature, as the lead did (D-074):
   ```sh
   cargo build --release --offline --locked -p m1-spike-app --features speech
   ```
   It links the renamed library from `~/lookout-data/cache/speech-macos`. Step 3.4 checks the
   binary before checks 6 and 7.
2. **Checks 1–4, no permission:**
   - 1, hotkey press and release, modifier-first included;
   - 2, a panel over another app that keeps focus;
   - 3, click-to-key focus;
   - 4, reveal on key-down and wake timing. It takes about 20 minutes, mostly waiting.
3. **Check 5, `cpal` on key-down:** Microphone. Grant it at the first prompt, then rerun if
   trial 1 recorded silence; the README says why.
4. **Checks 8 and 10, the window-only capture and the clock agreement:** Screen Recording, two
   terminals.
5. **Check 9, `CGEventPost` injection:** Accessibility; with `--tap`, also Input Monitoring.
   Don't touch the keyboard while it runs.
6. **Check 11, re-consent:**
   - `first-run` and `repeat` on day 1, then `after-reboot` and `next-day` on later days;
   - then the bundled helper (`scripts/bundle-sck-helper.sh`), the identity the overnight
     harness would use.
   - A prompt on a repeat run is the PRD §18.1 risk: stop and report.
7. **Checks 6 and 7, Swift partials and network silence:** step 3, after check 5. It starts with
   the speech library's model check, and runs on the speech build of the spike from step 2.1.
   - **Don't run the Build block again after that rebuild.** Its `cargo build --release
     --workspace` rebuilds `m1-spike` without the speech feature, and checks 6 and 7 then record
     `skipped`. Step 3.4 checks the binary, and says how to rebuild it if needed.

**Stop at the first `FAIL`.** PLAN.md M1 1a: run `m1-report show <run-id>` and send it with the
run file before going on.

**Report back, for every run:**
- the output of `m1-report list`, and `m1-report show <run-id>` for each run;
- the run files themselves, `~/snow-globe-data/spikes/m1/<run-id>.jsonl`. They hold no audio, no
  transcripts, no frames and no names; the spike README lists exactly what they hold;
- three observations the checks can't make for you:
  - check 1: does a modifier-first release fire when the modifier goes up, or when Space does?
  - check 2: which window level kept the panel above a full-screen app?
  - check 11: did any prompt appear?

The lead copies the numbers into DECISIONS.md (gate evidence: "Spike notes and numbers in
DECISIONS.md").

## 3. The speech library's first real run, then spike checks 6 and 7 (Mon 23 Nov)

The library is ready (D-055, renamed in D-080), and step 2.1 rebuilt the spike against it, as
the lead did at 12:36 UTC on 20 Nov (D-074). Run this in the spike terminal of step 1, after
check 5, so Microphone is already granted. The details are in [app/speech-macos/README.md](../../app/speech-macos/README.md), "What
needs the owner", and in the spike README, checks 6 and 7.

1. **Check the on-device model.** No permission is needed, and nothing is downloaded or recorded:
   ```sh
   T=~/lookout-data/cache/speech-macos/release/snow-globe-transcribe
   $T status --locale en-US
   ```
   - It prints the library's status code (0 ready, 1 model not installed, 2 Speech Recognition
     denied or restricted, 3 locale unsupported) and a `reservations` line: the model
     reservations held for this tool's identity, and the most allowed. It never prompts and never
     downloads.
   - Keep both lines for your report.
2. **Only if `status` reported code 1, install the model:**
   ```sh
   $T install-assets --locale en-US --yes
   ```
   - This downloads Apple's dictation model from Apple's servers. It is the only step here that
     makes the system fetch anything, and it does nothing without `--yes`.
   - It prints a `reservations` line before the download and another after. Keep both, with the
     `status` lines from before, and run `$T status --locale en-US` again afterwards.
   - Let the download finish before check 6. A download while check 7 watches the network makes
     its run inconclusive.
3. **"Not determined" must show no prompt.** The library reads the Speech Recognition setting and
   never asks for it (D-055), because Apple's documentation says that permission applies only to
   `SFSpeechRecognizer`. If System Settings > Privacy & Security > Speech Recognition doesn't
   list your spike terminal, the setting is "not determined" for it. Note whether any prompt
   appears during `status`, `install-assets` or check 6.
   - If `status` reports code 2, the setting is denied or restricted, and the library honours
     that. Switch it on for the terminal in that pane, and say so in your report.
4. **Check that the spike is a speech build of the renamed library.** `m1-spike` in the spike
   README's `$BIN` (`~/lookout-data/cache/cargo-target/release`) should be step 2.1's build
   with `--features speech`. The lead's build of 12:36 UTC on 20 Nov, under the old names, was
   12,991,256 bytes (D-074). This needs no permission:
   ```sh
   strings -a ~/lookout-data/cache/cargo-target/release/m1-spike | grep 'snow-globe-speech 0.3.0'
   ```
   - It should print one line: `snow-globe-speech 0.3.0 (SpeechAnalyzer + DictationTranscriber,
     on device only; macOS SDK 260200)`.
   - If it prints nothing, the spike was rebuilt without the speech feature, for example by the
     Build block after step 2.1. Rebuild it as in step 2.1, then check again:
     ```sh
     cd spikes/m1-platform
     CARGO_TARGET_DIR="$HOME/lookout-data/cache/cargo-target" \
       cargo build --release --offline --locked -p m1-spike-app --features speech
     ```
5. **Run check 6, which runs check 7 beside it:** `$BIN/m1-spike speech`, 5 trials. Wait 3 s for
   the network monitor, then hold the hotkey, say a place and a time ("Mount Rainier at sunset
   tomorrow") and let go (spike README, check 6).
   - The spike sets no silence pad, so these trials run with the library's default of 0 ms
     (D-067). That is expected.
6. **If network silence comes out inconclusive,** turn Wi-Fi off and run check 6 again, as the
   spike README's check 7 says. Send both runs.
7. **Afterwards,** run `$T status --locale en-US` once more and keep its `reservations` line: the
   lead compares the lines from before and after the first run to learn which identity model
   reservations follow (D-055). Then switch Microphone off, and Speech Recognition if you switched
   it on, until the recording tool or the app asks.

**Report back:**
- every `status` line and every `reservations` line: before, after `install-assets` if you ran
  it, and after check 6;
- whether the model had to be downloaded, and whether the download had finished before check 7;
- whether any prompt appeared while Speech Recognition was "not determined";
- as in step 2: `m1-report show <run-id>` for each run, and the run files.

## 4. The recordings

Waits for the recording tool (`TODO(M1)`, M1-09). Everything about reading the script is in
[bench/README.md](../../bench/README.md).

1. **The scripted set: 130 lines, about 20 minutes** (PRD §1 row 14). Read d01–d30, then
   t001–t100, exactly as written, odd spellings included.
   - Say the numbers as `python3 -I -B bench/utterances/check.py --reading-list` spells them
     beside each line ("1:30" is "one thirty").
   - A quiet room, the built-in microphone, and nobody else's voice.
   - Hold the hotkey, read, release, as you would for real.
   - Recordings go to `~/snow-globe-data/fixtures/voice/scripted/<script-hash>/`, never into the
     repo or iCloud.
2. **Consent:** the tool asks you twice, and records each answer in
   `~/snow-globe-data/fixtures/voice/consent.jsonl`:
   - whether the set may be replayed through on-device engines;
   - separately, whether it may be streamed to Deepgram in the bake-off (step 5). Answer that
     one only after you have approved Deepgram's first call.
3. **Real use: ≥ 30 utterances over the following days,** each kept only if you say yes to that
   one when the app offers. You confirm its transcript before it is stored. They are reported
   apart from the scripted set.

**Report back:** the script hash, the number of kept takes (for example 130 of 130), and
anything that went wrong. Later, the number of real-use utterances you kept.

## 5. The Deepgram key (approved for the bake-off only)

Deepgram is approved for the bake-off only (D-012 row 7, D-015): engine D, measured beside the
on-device default. Its spend has a hard cap of $3 (PRD §17), inside the sign-up credit.
1. **Create the account and a key,** when you're ready. Choose the least access that still lets
   a key mint short-lived tokens.
   - PRD §7.1: the app never holds a long-lived key. If no token API exists, there is no cloud
     path.
   - The bake-off code that mints tokens is M1 work (`TODO(M1)`, M1-06).
2. **Put the key only in the environment** of the process that mints tokens, for example
   `export DEEPGRAM_API_KEY=…` in a shell you open for that alone. The variable's name is fixed
   when the code lands.
   - Never export it in a shell that runs a coding agent, or in a shell profile that every new
     shell reads. Any program started from that shell sees the key, and the paid-call rule
     treats a present key as half of the permission.
   - `unset` it, or close that shell, when the bake-off is done.
   - Never put it in a file in the repo or in `.env`, and never in the app (CLAUDE.md
     "Secrets").
3. **You will be asked again before the first paid call,** even with the key in place (PLAN.md
   Rules; PRD §17). Every request sets `mip_opt_out=true` (D-015).
   - The bake-off streams your scripted recordings to Deepgram, so it also needs your
     `replay-deepgram` consent from step 4.
   - Each charge goes into the DECISIONS.md spend ledger.

**Report back:** that the key exists and where it is set, never the key itself. Then your yes or
no when the first call is proposed.

## 6. Overnight idle runs

Waits for:
- the harness and its one command (`TODO(M1)`, M1-09);
- the recordings (step 4);
- a frozen build: the lead builds it, records its commit, and nothing is rebuilt between nights.

What PRD §10.6 asks for (ticket M1-12):
- **≥ 100 idle trials** for the shipped engine and **≥ 60** for the comparison engine, spread
  over at least 3 days, with the engines interleaved.
- Each idle trial comes after ≥ 30 minutes with no query.
- So a night gives at most two trials an hour. The 160 trials need about 80 hours of
  unattended running: roughly 8 nights of 10 hours.
- The lead plans the nights with you, because M1 has 14 days.

**The lead has decided how idle trials treat the display (D-068):**
- Idle trials keep the system awake with `caffeinate -i`, **never `-d`**, and let the display
  sleep as the Mac's own settings do, as in real use. Every idle trial comes after ≥ 30 minutes
  with no query, and on AC power this Mac turns its display off after 10 minutes
  (`pmset -g custom`, 20 Nov), so the display is usually off when an idle trial starts.
- Don't change the display or Lock Screen settings to keep it on.
- Each trial's trace records whether the display was on (`TODO(M1)`). Trials with the display
  asleep are reported apart, as well as inside the totals: the time from key-up to the composited
  pixel (PRD §10.4) then includes waking the display.
- Follow-up rows may be idle trials too, since PRD §7.4 sets no expiry for the last place asked
  about. They are reported as idle follow-ups.

**Each evening:**
1. **AC power, lid open.**
   - `caffeinate -i` holds off idle sleep only while its command runs. Closing the lid sleeps
     the Mac anyway.
   - The capture loop's own `caffeinate -s` holds only on AC power. On 19 Nov, on battery, the
     Mac kept going into idle sleep (12 "Idle Sleep" entries in `pmset -g log` that day), and
     capture lost about 8.2 h (PRD_PROGRESS.md).
   - At 03:52 UTC on 20 Nov it also slept for about 3½ minutes with the lid closed, on battery.
2. **Quit other apps** and turn on Do Not Disturb: Control Center > Focus > Do Not Disturb.
3. **Check Low Power Mode is off,** because it slows the CPU.
   - In System Settings > Battery, Energy Mode should read Automatic, not Low Power.
   - Or read it without opening anything:
     `pmset -g custom | awk '/^AC Power/{ac=1} /^Battery Power/{ac=0} ac && /lowpowermode/'`
     prints the AC Power line only, and it should end in 0. It did on 20 Nov.
4. **Leave the capture loop and the forecast job running.** They are never stopped (PLAN.md cut
   order: "never cut"). The report notes them as background load.
5. **Start the harness** under `caffeinate -i` (never `-d`, D-068), as the lead gives you the
   exact command; then leave the Mac alone until morning.

**The first night is also a dry run** for two open questions, so don't change your security
settings to get around either. If a setting has to change, the lead will name it.
- **Does paint capture still work once the display sleeps or the screen locks?** The display
  sleeps every night (D-068), so this matters for every trial that starts with it off.
- **Does any Screen Recording prompt appear?** That is check 11's next-day scenario, live.

**Each morning, report back:**
- the summary line the harness prints: trials completed per engine, and any gaps;
- whether the Mac slept (`pmset -g log | grep -E "Sleep|Wake"` for the night);
- anything you noticed, such as a prompt, a crash, an update notice or a warm case.

Send no audio; the traces hold timings only (PRD §10.5).

## 7. Decisions the lead will bring you in M1

- **The parser's rule readings** (D-039, owner action 9): "tonight", dates, the same day, "next
  friday", "tomorrow" = 12:00, and a lone generic word as a miss.
- **Rule questions the utterance script turned up,** in
  [bench/README.md](../../bench/README.md), "Rows waiting on the owner". They are rules, never
  one-row fixes:
  - a name close to an alias, found only by fuzzy matching ("Tacoma"): painted, refused, or
    offered on a chip, and what counts as close;
  - a place name plus a word left over ("Yosemite Valley", "Paradise Valley"): D-039's fallback,
    or something else;
  - a lone generic word ("Grand"): close-call chip or miss.
- **New readings to confirm,** in the same file, "Readings the PRD doesn't make":
  - dawn and dusk as civil twilight;
  - "tonight at 9" asked at 21:30 going to tomorrow at 21:00, as PRD §7.4 says literally;
  - whether "tomorrow" follows the place's date or yours;
  - whether the 48 h and 7-day horizons count from now or from the newest usable model run;
  - what "the last place asked about" is after a miss.
- **Accessibility for injection** (D-033).
- **The STT choice** (D-015). The default stays on device unless you switch after seeing the
  bake-off numbers (PRD §7.1).

## What M1 needs from you, in one list

| When | What | Waits for |
|---|---|---|
| now | AC power; no macOS upgrade; some free disk | — |
| Mon 23 Nov | Spike checks 1–5 and 8–11 (one day; check 11 runs over several days), and their permissions | the lead's switch after the M0.5 gate (D-080) |
| Mon 23 Nov, after check 5 | The speech library's first real run (`snow-globe-transcribe status`, and `install-assets` only if it reports code 1), then spike checks 6 and 7 (step 3) | the lead's switch and its rebuild under the new names (D-080); step 2.1 then rebuilds the spike with the library (D-074) |
| when the recording tool lands | The 130 scripted recordings (about 20 min) and the consent answers; then ≥ 30 real-use ones | M1-09 |
| when you're ready | The Deepgram account and key; then a yes or no to the first call | your decision |
| after the recordings | About 8 nights of idle runs under `caffeinate -i` (never `-d`, D-068), from a frozen build (M1-12) | M1-09, M1-10 |
| during M1 | The decisions in step 7 | the lead |
