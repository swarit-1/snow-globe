# M1 owner runbook: what you do in M1, in order

**Updated:** 2026-11-20 (UTC).

This is everything M1 (Voice to first pixel, Mon 23 Nov → Mon 7 Dec 2026) needs from you, the
owner, in the order it is needed. Each step says what to run and what to send back. Details live
in the documents it links to; this page only sequences them.

**State on 20 Nov:**
- The 1a spike app is built and unit-tested, and none of its checks has run (D-033).
- The Swift speech library (`app/speech-macos/`, ticket M1-05) is being written; nothing of it is
  committed yet. The replay harness and the recording tool (M1-09) are not started.
- So steps 3 to 6 below wait for code; each says what it waits for.
- If M0.5 or M1 runs late, the plan puts the freeze-critical work first (PLAN.md, Calendar).
  The speech and harness steps may then wait; the lead will tell you.

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
   still run on this laptop (D-030), and every M1 measurement assumes AC power.
2. **Don't upgrade macOS.** It stays on 26.5.x through M4 (PRD §1 row 1); an update mid-milestone
   breaks the latency comparisons.
3. **Free some disk** if you can (owner action 11). `df` showed 21 to 23 GiB available between
   04:03 and 06:11 UTC on 20 Nov, and the figure moves as builds and capture run. Apple
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
| Speech Recognition | System Settings > Privacy & Security > Speech Recognition | spike check 6 (if the speech library asks); the app | step 3 |
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
   `CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target`.
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
7. **Checks 6 and 7, Swift partials and network silence, wait** for the speech library
   (`TODO(M1)`, M1-05; not committed yet).
   - Then: build with `--features speech`, run `m1-spike speech`.
   - If network silence comes out inconclusive, run it again with Wi-Fi off. The README explains
     both.

**Stop at the first `FAIL`.** PLAN.md M1 1a: run `m1-report show <run-id>` and send it with the
run file before going on.

**Report back, for every run:**
- the output of `m1-report list`, and `m1-report show <run-id>` for each run;
- the run files themselves, `~/lookout-data/spikes/m1/<run-id>.jsonl`. They hold no audio, no
  transcripts, no frames and no names; the spike README lists exactly what they hold;
- three observations the checks can't make for you:
  - check 1: does a modifier-first release fire when the modifier goes up, or when Space does?
  - check 2: which window level kept the panel above a full-screen app?
  - check 11: did any prompt appear?

The lead copies the numbers into DECISIONS.md (gate evidence: "Spike notes and numbers in
DECISIONS.md").

## 3. After the speech library lands

When the lead says `app/speech-macos/` builds:
1. Run spike checks 6 and 7 as in step 2.7. Grant Speech Recognition if asked. Check 6 also
   reports whether the en-US dictation asset is installed.
2. If the asset is missing, its download is Apple's, over the network. It must finish before
   the network-silence run counts; say so in your report if it happened.

**Report back:** as in step 2, plus whether the asset had to be downloaded.

## 4. The recordings

Waits for the recording tool (`TODO(M1)`, M1-09). Everything about reading the script is in
[bench/README.md](../../bench/README.md).

1. **The scripted set: 130 lines, about 20 minutes** (PRD §1 row 14). Read d01–d30, then
   t001–t100, exactly as written, odd spellings included.
   - Say the numbers as `python3 -I -B bench/utterances/check.py --reading-list` spells them
     beside each line ("1:30" is "one thirty").
   - A quiet room, the built-in microphone, and nobody else's voice.
   - Hold the hotkey, read, release, as you would for real.
   - Recordings go to `~/lookout-data/fixtures/voice/scripted/<script-hash>/`, never into the
     repo or iCloud.
2. **Consent:** the tool asks you twice, and records each answer in
   `~/lookout-data/fixtures/voice/consent.jsonl`:
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

**Before night 1, the lead decides one thing and records it in DECISIONS.md:** whether idle
passes keep the display awake.
- Every idle trial comes after ≥ 30 minutes with no query. On AC power this Mac turns its
  display off after 10 minutes and would sleep after 1 (`pmset -g custom`, 20 Nov), and
  `caffeinate -i` prevents only the sleep. So, unless something keeps it on, the display is
  off at the start of every idle trial.
- The time from key-up to the composited pixel (PRD §10.4) would then include waking the
  display, if paint capture works at all with it off. That mixes the idle headline with a
  wake state.
- The choices: keep the display on for idle passes (`caffeinate -di`, or System Settings > Lock
  Screen > "Turn display off on power adapter when inactive" set to Never), or let it sleep and
  report those trials as their own stratum.
- Either way, each trial's trace records whether the display was on (`TODO(M1)`), and trials
  with the display off are reported apart.

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
5. **Start the harness** under `caffeinate -i` (with `-d` too if the lead chose to keep the
   display on), as the lead gives you the exact command; then leave the Mac alone until morning.

**The first night is also a dry run** for two open questions, so don't change your security
settings to get around either. If a setting has to change, the lead will name it.
- **Does paint capture still work once the display sleeps or the screen locks?** This matters
  only if the lead chose to let the display sleep.
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
| Mon 23 Nov | Spike checks 1–5 and 8–11 (one day; check 11 runs over several days), and their permissions | nothing |
| when the speech library lands | Spike checks 6 and 7 | M1-05 |
| when the recording tool lands | The 130 scripted recordings (about 20 min) and the consent answers; then ≥ 30 real-use ones | M1-09 |
| when you're ready | The Deepgram account and key; then a yes or no to the first call | your decision |
| after the recordings | About 8 nights of idle runs under `caffeinate -i`, from a frozen build (M1-12) | M1-09, M1-10, the lead's display decision |
| during M1 | The decisions in step 7 | the lead |
