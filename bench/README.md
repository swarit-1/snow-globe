# Bench: the scripted utterances for the M1 replay harness

**Updated:** 2026-11-23 (UTC).

Renamed for Snow Globe on 21 Nov 2026 (D-080): the parser is `snow-globe-parse` in the crate `snow-globe-core` (were `lookout-parse` and `lookout-core`); the parser outputs recorded on 20 Nov came from the old binary at `8246a8b`, which differs only in name. `$SNOW_GLOBE_PARSE` (or the old `$LOOKOUT_PARSE`) names the binary.

**Nothing has been recorded.** This folder holds the script the owner will read and the
instructions for reading it. There is no audio here, and there never will be: recordings live
outside the repository, under `$SNOW_GLOBE_DATA_DIR/fixtures/voice/` (see [Where recordings
live](#where-recordings-live)). The recording tool does not exist yet (`TODO(M1)`, ticket M1-09
in [PRD_PROGRESS.md](../PRD_PROGRESS.md)), so the steps below describe how a recording session
will run once it does.

What this is for (PRD §10.6, §14): the replay harness plays each recording through the live audio
path and measures, idle first, the time from key-up to the first painted frame, the first
partial, release-to-final, WER, place accuracy after the parser, the fallback rate and the
wrong-paint rate. The script is the fixed input those numbers are measured on.

## What is here

| File | What |
|---|---|
| [utterances/scripted.tsv](utterances/scripted.tsv) | The 130 scripted utterances (30 dev, 100 test), with the expected query for each and what snow-globe-core's parser printed |
| [utterances/clocks.tsv](utterances/clocks.tsv) | The six reference clocks the expected times are computed at |
| [utterances/prd_reading.py](utterances/prd_reading.py) | The source of the script: each row's text, tags and expected result as read from PRD §7.4 and §7.5. It writes both TSVs. |
| [utterances/check.py](utterances/check.py) | Validates the script, reruns `snow-globe-parse` on every row, compares, and checks the stored verdicts |
| [utterances/test_utterances.py](utterances/test_utterances.py) | Tests for both scripts (stdlib `unittest`) |
| [replay/](replay/README.md) | The replay scorer and its report (PRD §10.6, ticket M1-09): schema-2 traces re-parsed at the context they record, every metric with a seeded cluster bootstrap. It has run only on synthetic runs; nothing is measured |
| [ruff.toml](ruff.toml) | Lint and format settings, the same rules as every `pyproject.toml` in the repo |

Everything here is Python standard library only, with no network and no audio:

```sh
python3 -I -B bench/utterances/check.py --no-parser     # validate the TSV and its stored verdicts
python3 -I -B bench/utterances/check.py                 # also rerun the parser and compare
python3 -I -B bench/utterances/check.py --reading-list  # the lines as the owner says them
python3 -I -B -m unittest discover -s bench/utterances -t bench/utterances -p 'test_*.py'
python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
ruff check bench && ruff format --check bench           # any ruff; the repo's is in workers/common/.venv
```

`check.py` needs the parser binary: `cd app && CARGO_TARGET_DIR=$HOME/lookout-data/cache/cargo-target cargo build --release --offline -p snow-globe-core --bin snow-globe-parse` (`~/lookout-data` is the data dir until the rename's live switch and a symlink to `~/snow-globe-data` after it, so the command is right before and after, and never creates a second data folder; D-080).
`make bench-script` does both: it builds the binary (offline and `--locked`), then runs
`check.py` on it (D-075). `make test` runs only `check.py --no-parser`.
- Without it, the parser tests are skipped, never faked.
- A binary older than anything it is built from (app/core's sources, the Cargo files, or the
  manifests it embeds) is refused the same way, so a rebuild must come first. Exit status 2.
- `check.py` exits 1 when the parser's output has drifted from the TSV, or when a stored
  `agreement` or `difference` is not what its comparison gives today. `--write` rewrites both.

## The script

**130 utterances: 30 for development and 100 for test** (PRD §10.6). The dev rows are for
building and debugging the harness and the parser. The test rows are for measuring only; see
[Split discipline](#split-discipline).

**Columns** (`check.py` documents each one):
- `id`, `split`, `text`, `tags`, `clock`, `context`;
- the expected result: `expected_outcome`, `expected_viewpoint_id`, `expected_rule`,
  `expected_local`, `expected_utc`, `expected_chips`, `tolerance_s`, `prd_note`;
- the parser's result: `parser_outcome`, `parser_viewpoint_id`, `parser_rule`, `parser_local`,
  `parser_utc`, `parser_chips`;
- the comparison: `agreement` and `difference`.

`expected_viewpoint_id` is a manifest id, `no-match` for a place the parser must refuse, or two
ids joined by `|` for a close call, where either is right but a chip must offer the other.

**Coverage** (checked by `check.py` and the tests):
- **Places.** All 8 viewpoints, and every one of the 77 manifest aliases at least once, among
  them the sound-alike aliases "point rays", "point raise", "mount rainer", "grand titan",
  "arcadia" and "yo semite".
  - Four names that are not aliases should still be found by the fuzzy and phonetic stages of
    PRD §7.5: "Point Raze", "Teton", "Smoky Mountain" and "Big Ben".
  - "Yosemite Valley" is a fifth, but its expected outcome waits on the owner (see
    [Rows waiting on the owner](#rows-waiting-on-the-owner)).
- **Every time rule in PRD §7.4:**
  - a bare hour ("at four"), with its AM/PM chip;
  - an hour with AM, PM, "tonight" or "this morning", including ones that have passed: AM (d07,
    t044, t067), PM (t041) and "tonight" (t030), each asked after the hour;
  - a day plus a bare hour, at the 6/7, 11 and 12 boundaries of the 7–11 AM, 12 noon, 1–6 PM rule;
  - now, and no time at all;
  - sunrise, sunset, dawn and dusk; morning and evening golden and blue hour;
  - the four parts of the day; "tonight" alone, before and after it has passed, and in midsummer
    when 1 h after sunset is later than 21:00;
  - relative times; weekdays (today's, a wrap to next week, "next <weekday>"); dates;
  - the 48 h HRRR horizon and the 7-day limit, on both sides;
  - follow-ups that name no place: "what about …", "and at …", and the back-references "same
    place" and "that spot".
- **Daylight saving:**
  - the repeated hour on 1 Nov 2026 in Pacific and Eastern time, including one ask made inside it;
  - the skipped hour on 14 Mar 2027 in Pacific, Mountain and Central time;
  - Phoenix across both changes;
  - and valid times whose offset differs from now's.
- **Cross-zone:** every row is asked from a Chicago clock. Several are only right if the
  place's zone is used: "Rainer at 11", "Pt Reyes at 10:30 am" and "Grand Canyon South Rim at
  11:30" have not happened yet at the place, but have already passed in Chicago.
- **Close calls:** "Grand" alone, and two places named at once.
- **Refusals:**
  - places Snow Globe doesn't know: Golden Gate, Mount Hood and Big Sur;
  - real places whose names come close to an alias: "Tacoma" and "Paradise Valley" (test),
    "Tetonia" and "Rainier Valley" (dev), all waiting on the owner;
  - an unknown viewpoint, and no place at all;
  - asks outside the grammar, which go to the fallback: "this weekend", "on Thanksgiving",
    "after sunset".

**Composition** (expected outcome, test split): 88 queries, 6 refusals as no match, 2 for the
fallback, and 4 out of range (2 past, 2 beyond). Rows per viewpoint, test split (a close call
counts for both of its places):
- Rainier 28, Point Reyes 16, Grand Canyon 10, Grand Teton 9, Yosemite 9, the Smokies 9,
  Acadia 8, Big Bend 6.
- The MSV pair carries the most rows on purpose. The scripted set is built for coverage, not as
  a sample of real use, so its fallback rate has a 2 % floor by construction. That is why PRD
  §10.6 also asks for real-use utterances, reported separately.

**Reference clocks** ([clocks.tsv](utterances/clocks.tsv)). Expected times are fixed by an
injected clock, never the wall clock:

| Clock | Now (Chicago, the asker's zone) | Rows | Why |
|---|---|---|---|
| C1 | Thu 19 Nov 2026, 12:00 CST (18:00Z) | 106 | The main clock; the PRD §7.3 example resolves to its exact JSON here |
| C2 | Thu 19 Nov 2026, 23:30 CST | 9 | Passed evening hours and events, midnight, and a place that is already on Friday |
| C3 | Sat 31 Oct 2026, 12:00 CDT | 6 | The day before the fall-back |
| C4 | Sun 1 Nov 2026, 01:15 PST (Los Angeles) | 1 | Inside the repeated hour |
| C5 | Sat 13 Mar 2027, 12:00 CST | 5 | The day before the spring-forward |
| C6 | Mon 21 Jun 2027, 12:00 CDT | 3 | Midsummer, for "tonight" after a late sunset |

One clock can't reach both DST changes within the 7-day horizon, so the DST rows have their own.

## How the expected columns were filled

1. **From the PRD, by hand.** [prd_reading.py](utterances/prd_reading.py) holds each row's
   reading of PRD §7.4 and §7.5, and turns it into times. It shares no code with snow-globe-core
   or oculus-core:
   - wall times go through the system tz database (2026d);
   - the Sun comes from Meeus's low-accuracy solar position, at the PRD §7.4 altitudes.

   Where PRD §7.4 is silent, the row's `prd_note` says which reading was used. D-039's proposed
   reading is followed where one exists, including its date and same-day rules, which no row
   uses yet. The exceptions are tagged `owner-pending` (below).
2. **From snow-globe-core's parser.** `check.py --write` ran `snow-globe-parse` (snow-globe-core at
   commit `8246a8b`; the binary's SHA-256 is `ed5c1815…`) on every row's text at its clock and
   context, and filled the `parser_*` columns.

**The first freeze (the builder, 20 Nov).**
- At 03:11:10 UTC the reading was hashed (`scripted.tsv` SHA-256 `ddb6a01a…`) and a copy kept in
  the workflow's scratch space.
- The builder states that `snow-globe-parse` had then run only on the seven PRD §3.3 examples. The
  artifacts show the freeze, not that order.
- Two caveats:
  - The reading was written after the parser's source and unit tests had been read, to learn
    its output format. 16 of the 130 original texts also appear word for word in
    `app/core/tests/parser_table.rs`; 5 of them are PRD §3.3 examples.
  - `prd_reading.py` itself changed after that freeze (SHA-256 `f725f6f6…` then), and no copy of
    that version was kept. Only the generated columns can be compared, and those were unchanged.

**Changes after the review (20 Nov).** The reviewer and fixer made these, after the parser's
results on the test rows were known. None was made to change an agreement:
- **Dev rows swapped in** for the rule questions that test rows raise (see [Split
  discipline](#split-discipline)):
  - d01 "Rainier Valley tomorrow at 9" replaced "Rainier tomorrow at 9";
  - d02 "Show me Tetonia at 8" replaced "Show me Mount Rainier at 8";
  - d06 "That spot in half an hour" replaced "Point Reyes in half an hour".
- **Two test rows replaced**, because no row had a passed PM hour or a passed "tonight at N":
  - t030 "Rainier tonight at 9" at C2 replaced "Rainier tonight" at C6;
  - t041 "Point raise at 8 pm" at C2 replaced "Point raise at 2 pm" at C1.
  - Both rows taken out agreed with the parser. The reviewer's probe had already shown that
    the parser and the reading agree on "Rainier tonight at 9" and "Rainier at 8 pm" at C2.
- **Follow-ups now come straight after a row that resolves to their place:**
  - t032 and t034 swapped: "And at sunrise?" now follows "Rainier sunset", not a miss;
  - t074 and t075 swapped: "Same place at dusk" now follows "Turtle back dome tomorrow at 2 am";
  - t005 "What about midnight?" now expects Grand Canyon, the row before, as in PRD §3.3. It
    expected Point Reyes before.
- **`owner-pending`** on d29, t035, t036 and t075 (and the new d01 and d02), with notes.
- **Notes:** the horizon reading on t012, t022 and t083; which row sets the place, on t032 and
  t074; the passed-hour reading on t030 and t041.

These changes were frozen before the parser ran on the changed rows: `scripted.tsv` SHA-256
`a40867e5…` at 05:37:07 UTC, with columns 1–14 unchanged since. The test split's text-level
score is the same before and after them: 94 of 100.
- **A caveat, as for the first freeze:** `prd_reading.py` changed after this second freeze too
  (its file time is 06:03:48 UTC). The freeze's record, `freeze2.sha256` in the workflow's
  scratch space, gives its SHA-256 as `19377fb8…`; the file committed in `dc28c2a` is
  `fb2c1430…`, and no copy of the frozen version was kept.
- Only the generated columns can be compared, and those are unchanged. Regenerated from the
  committed `prd_reading.py` in a scratch copy of the repo (20 Nov, about 15:45 UTC), columns 1–14
  of `scripted.tsv` match the second freeze's copy (`a40867e5…`) and the committed file
  (`8c1d7481…`) row for row, and `clocks.tsv` is byte for byte the committed one (`039c25c8…`).

**Result: 121 rows agree, 4 differ softly and 5 disagree.** On the test split alone: 94 agree,
3 soft and 3 disagree; leaving out its 3 `owner-pending` rows, 94 agree, 2 soft and 1
disagrees, of 97.
- **Solar-based times:** the parser and the independent Sun agree within 1 s on all 28 rows
  (mean 0.25 s; 21 rows at 0 s, 5 at +1 s and 2 at −1 s). The parser rounds up to the second,
  the reading to the nearest, which can only add 0 or +1 s. The −1 s rows (t082, t093) show that
  the two Sun models also differ by up to about half a second.
- **Clock times:** all 86 agree to the second, offsets included.

| Row | Text | PRD reading | Parser | Note |
|---|---|---|---|---|
| t036 | Tacoma at sunset | no match: a real city (§7.5, "never a guess") | Rainier, fuzzy 0.83, no chip | `owner-pending`. The one wrong place painted. |
| t075 | Yosemite Valley at sunset | Yosemite | fallback: "valley" left over | `owner-pending`; D-039 sends leftover words to the fallback |
| t074 | Same place at dusk (after Yosemite) | Yosemite at dusk (§7.4: a time with no place goes to the last place) | no match on "same place" | A parser gap: "there" is a filler word; "same place" and "that spot" are not. d06 is the dev row for it |
| d06 | That spot in half an hour (after Point Reyes) | Point Reyes in 30 minutes | no match on "that spot" | The dev row for t074's rule |
| d29 | Grand at sunset | a close call, Grand Canyon or Grand Teton with a chip (§7.5) | no match | `owner-pending`: D-039 reads a lone generic word as a miss |
| t035 | Paradise Valley at 9 | no match | fallback | `owner-pending`. Soft: both refuse; only the fallback rate differs |
| d01 | Rainier Valley tomorrow at 9 | no match | fallback | `owner-pending`. Soft, as t035 |
| t022 | Rainier Thursday at 9 am | next week, no extra chip | the same, plus `rolled_to_next_week` | Soft: PRD §7.4 defines no chip for it |
| t064 | Tetons tonight (at 22:30) | the next whole hour (§7.4 is silent) | the same, plus `rolled_to_next_hour` | Soft |

d02 "Show me Tetonia at 8" is `owner-pending` too; the parser and the reading both refuse it.

### Rows waiting on the owner

A row is tagged `owner-pending` when its expected outcome rests on a judgement that neither PRD
§7.5 nor D-039 makes, or goes against D-039. No matcher that follows PRD §7.5 can pass t035,
t036 and t075 together without a list of particular names, and scoring against them would push
the parser toward exactly that. So until the owner rules:
- the rows stay in the script with the reading's current guess, and `check.py` lists them apart;
- the replay report gives "exact place and time" on the other rows and lists these separately
  (see [Scoring](#scoring-proposed-for-the-lead));
- after the ruling, the reading is changed by rule for every row it touches, the tag comes off,
  and the commit says so.

The questions are about rules, not rows:
1. **A name close to an alias, when only the fuzzy stage matches** ("Tacoma" against the alias
   "tahoma"; "Tetonia" against "tetons"). Is the place painted, refused, or painted with a
   close-call chip? What sets "close"? Whatever rule is chosen also decides the rows the reading
   expects fuzzy or phonetic matching to find ("Big Ben", "Teton", "Smoky Mountain", "Point
   Raze"). Rows: t036, d02.
2. **A place name plus a word left over** ("Yosemite Valley", "Paradise Valley", "Rainier
   Valley"). D-039 sends every leftover word to the fallback. The reading instead matched the
   valley the camera looks over and refused the other places, which no rule can do without a
   list. Rows: t075, t035, d01.
3. **A lone generic word** ("Grand"): PRD §7.5's close-call chip, or D-039's miss? Row: d29.

Back-references ("same place", "that spot") are not a question: PRD §7.4 sends a time with no
place to the last place asked about. The parser doesn't know them yet. The rule is to be built
on d06, and t074 measures it.

### Readings the PRD doesn't make

Here the parser and the reading agree, but the owner should confirm. D-039 lists the first
group; the second group is new.
- **In D-039 already:**
  - a day said alone is 12:00 (t021, t068, t072, t083);
  - "next <weekday>" is the next such day, so on a Thursday "next Friday" is tomorrow (t021,
    t047, t083, t091);
  - "tonight at 1–3" is after midnight, and 4 and 5 get an AM/PM chip (d17, t088, t098);
  - asked inside the repeated hour, a repeated time takes its second pass (t029).
- **Not in D-039:**
  - PRD §7.4 has no dawn or dusk rule; both read civil twilight, −6° (t043, t070, t074, t097).
  - "tonight" asked after it has passed is the next whole hour (t064).
  - An hour said with "tonight" that has passed goes to tomorrow, as PRD §7.4 row 2 says
    literally. So "Rainier tonight at 9" asked at 21:30 is tomorrow at 21:00 (t030), though most
    people would mean the hour just gone.
  - A part of the day or noon that has passed today is tomorrow's (d13, t077, t079, t092). PRD
    §7.4 states that roll only for an hour said with AM, PM, "tonight" or "this morning".
  - "live" means now (d23).
  - **"tomorrow" is the place's tomorrow** (§7.4, "local to the place"). Asked at 23:30 in
    Chicago, "Acadia tomorrow at 9" is Saturday, because Acadia is already on Friday, while the
    asker means Friday (d20). Likewise "next Thursday" is in range at Acadia (6 d 23 h, t083)
    and out of range at Rainier (7 d 2 h, t021).
  - **Horizons are counted from now** (both sides). PRD §8.3 defines lead time from a model
    run's availability, and only the 00/06/12/18Z runs reach 48 h. Counted from the newest
    usable run instead, three rows change at C1 (18:00Z):
    - the newest 48 h HRRR run then is 12Z (its f48 p95 lag is 116.65 min, PRD_PROGRESS.md), so
      HRRR ends at 12:00Z on Sat 21 Nov, and t012 (Sat 09:00 PST, 47 h ahead) would get the GFS
      caption;
    - t022 and t083 (Thu 26 Nov 17:00Z, 167 h ahead) would be out of range if the 7 days run
      from the 12Z GFS run's start. They would sit at the limit if counted from its
      availability, with an assumed 5 h lag that has not been measured.
  - **"The last place asked about" after a miss** is not defined: the place of the last query
    that resolved, or none? The script no longer depends on it: every follow-up comes straight
    after a row that resolves, and t100 is played first after a launch.

**A text-level number, not the replay metric.** Against this reading, the parser alone gets
94 of the 100 test rows exactly right on the script's own text, or 97 counting the soft
differences.
- Both numbers are the same before the review changes (the builder's run, about 03:20 UTC on
  20 Nov) and after them. Leaving out the 3 `owner-pending` test rows: 94 of 97 and 96 of 97.
- The parser also refuses 4 of the test rows to the fallback (t035, t048, t075, t090); the
  reading expects 2 (t048, t090).
- PRD §14's "≥ 95 % exact place and time" is measured on what the speech engine heard, so it
  needs the recordings. This is only the ceiling the parser sets before any speech error, under
  a reading the owner hasn't confirmed.

## Where recordings live

```
$SNOW_GLOBE_DATA_DIR/fixtures/voice/            default <data dir>/fixtures/voice/, outside iCloud and git
  scripted/<script-hash>/<id>.<take>.wav     one file per take, e.g. scripted/3b4256f1cf51/t001.1.wav
  scripted/<script-hash>/<id>.<take>.json    its sidecar
  real-use/<YYYYMMDDTHHMMSSZ>.wav            an opt-in real-use utterance, named by its UTC key-down time
  real-use/<YYYYMMDDTHHMMSSZ>.json           its sidecar, with the reference transcript you confirm
  consent.jsonl                              one line per opt-in decision, never edited
```

- **Never in the repository.** `.gitignore` ignores every common audio format and any
  `fixtures/voice/` folder, but that is a backstop, not the plan.
- **Never in iCloud.** `~/Documents` syncs; the data dir (`~/lookout-data` until the rename's live switch, then `~/snow-globe-data`) doesn't (D-017, D-080).
- **`<script-hash>`** is what `check.py --no-parser` prints, today `3b4256f1cf51`: the first 12
  hex digits of SHA-256 over each row's id, split and text.
  - Changing what is read starts a new folder, so a recording is never scored against a line it
    wasn't read from.
  - Fixing an expected reading or the parser doesn't change the hash.
- **`<take>`** counts from 1. A take you reject is deleted at once (PRD §15.1 keeps only the
  recordings you add to the replay set). The harness replays the latest kept take.
- **Audio format:** WAV exactly as the live path captured it:
  - the input device's own channel count and sample rate, 32-bit float, the format `cpal`
    delivers;
  - no resampling, downmix, gain or noise processing. The spike's speech path mixes to mono
    only on the way to the speech library ([audio.rs](../spikes/m1-platform/tauri-app/src/audio.rs));
    the fixture keeps what the device gave.

  The harness swaps only sample values inside the live `cpal` callback (PRD §10.6), so it must
  refuse a fixture whose channel count or rate differs from the stream it replays into
  (`TODO(M1)`). Spike check 5 reports both for this Mac.
- **Size:** 130 takes of about 3 s at 48 kHz come to about 75 MB per channel.
- **Sidecar** (`TODO(M1)`, written by the recording tool):
  - `id`, `take`, `script_hash` and `recorded_at` (UTC);
  - channel count, sample rate, sample format, frames and duration;
  - key-down and key-up offsets, plus speech onset and end, all in ms. The onset is what "first
    partial ≤ 300 ms after speech onset" is measured from (PRD §14). Whether you were still
    speaking at key-up, a PRD §10.6 report line, follows from them. Their names are
    `key_down_ms`, `key_up_ms`, `speech_onset_ms` and `speech_end_ms`, counted from the
    recording's first sample; the scorer reads the last three, with `id`, `take` and
    `script_hash` ([replay/traces.py](replay/traces.py));
  - the input's kind (built-in or external), never a device name;
  - the consent scope, below.

  No transcript of a scripted take is stored: its reference is the script line.

## Recording, when the tool exists

1. **Who:** you, the owner, only. Nobody else's voice goes into the replay set (CLAUDE.md
   "Audio"). If someone else speaks, or a TV or call is audible, redo the take.
2. **Where and with what:** a quiet room, and the microphone the idle trials will use, the
   MacBook Air's built-in one unless the lead says otherwise. One speaker, one microphone and
   one room is a known bias that the report states (PRD §10.6).
3. **How:** hold the hotkey, read the line, release, as you would ask Snow Globe for real.
   - Don't pause before speaking or before releasing: the report measures how often you are still
     speaking at key-up, and an unnatural pause would hide it.
   - The REC indicator shows while you hold the key and switches itself off when you release
     (PRD §10.6, §15.2).
4. **Read each line exactly as written, odd spellings included.** They are written the way a
   transcriber might spell a place. Say them as spelt:
   - "Mount Rainer" as RAY-ner, "Yo Semite" as YO-seh-mite;
   - "Point Raze", "Big Ben", "Grand Titan", "Arcadia", "Tacoma" and "Tetonia" as those words;
   - "Mt" is "Mount" and "Pt" is "Point".
5. **Numbers:** say them the way `check.py --reading-list` spells them out beside each line.
   These are the forms in today's script:
   - plain hours as words: "4" is "four", "12" is "twelve" (not "noon"), "24 hours" is
     "twenty-four hours";
   - "1:30" is "one thirty" and "11:30" is "eleven thirty", not "half past";
   - "am" and "pm" as letters: "2:30 am" is "two thirty a.m.", "10:30 am" is "ten thirty a.m.",
     "8 pm" is "eight p.m.";
   - dates as ordinals: "the 22nd" is "the twenty-second", "November 21st" is "November
     twenty-first", "December 1st" is "December first".
6. **Order:** d01–d30 first, then t001–t100. That is about 20 minutes (PRD §1 row 14); take
   breaks whenever you like. If you misread, stumble or cough, redo the take.
7. **Then:** the tool lists the kept takes against the script, and you report the count, for
   example 130 of 130, to the lead.

The tool itself, the sample substitution and the per-pass cache namespaces are M1 work
(`TODO(M1)`, PLAN.md M1 1c). The scorer is built ([replay/](replay/README.md)) and has run only on
synthetic runs.

## Scoring, proposed for the lead

[replay/](replay/README.md) implements these rules; its README gives every metric with its
denominator.

- **Place and time accuracy:** re-parse the engine's final transcript with snow-globe-core at the
  row's reference clock, then compare with the expected columns as `check.py` does. The live
  runs keep the real clock, for latency.
- **Follow-ups are measured, not supplied.**
  - The harness plays each follow-up as the trial straight after the row before it in script
    order, in the same app session and engine. `check.py` makes sure that row resolves to the
    follow-up's `context` place.
  - The trace records the place the app actually used as its last place: trace schema 2's
    `context`, as manifest ids and never text ([trace.rs](../app/core/src/trace.rs)). The scorer
    re-parses at that live context, never at the `context` column. A live context that differs
    from the column makes the row wrong on place.
  - The report also counts follow-ups whose live context differed, and why (the row before was
    misheard, or the app lost it), so one mishearing isn't hidden.
  - A follow-up played ≥ 30 min after the row before is an idle trial only if the app keeps its
    last place that long; PRD §7.4 sets no expiry. Otherwise follow-ups are warm trials and are
    reported as such. The lead decides which.
- **t100 "Show me the sunset"** expects a miss only when there is no last place, so it is played
  as the first query after a launch, and the trace must show an empty context.
- **`owner-pending` rows** are reported apart from the "exact place and time" figure until the
  owner rules. The figure names its denominator. Rows still marked `no` are reported apart until
  the lead resolves them.
- **WER**, reported twice: raw against the text, and normalised.
  - Normalised, both the reference and the engine's output first have their numbers written out
    the way `check.py`'s `spoken()` does: "4", "four" and "4:00" all count as "four", and "a.m.",
    "am" and "a m" as one word. Any alias counts as its place's name, and "Mt" as "Mount".
  - The normaliser is [replay/wer.py](replay/wer.py).
- **Display state** (D-068): each trace records whether the display was on when the query
  began (schema 2's `state.display`); the report gives display-asleep trials apart and inside
  the totals.
- **Transcripts** the scorer needs (WER and the re-parse) stay out of the trace, which never
  holds text (PRD §10.5). They go beside the run's results under `$SNOW_GLOBE_DATA_DIR`, outside git
  and iCloud, with the recordings' retention. The scorer reads them as `transcripts.jsonl`
  ([replay/README.md](replay/README.md)); writing them is the harness's job (`TODO(M1)`).

## Real-use utterances (≥ 30, PRD §10.6)

- **Opt-in, one by one.** After a real query, a later M1 build can offer to add that utterance to
  the replay set (`TODO(M1)`). The default is no, and nothing is kept unless you say yes to that
  utterance.
- **The reference transcript** is yours: type it, or correct the engine's, before it is stored
  beside the audio, outside git.
- **Reported separately** from the scripted set (PRD §10.6).

## Consent and retention (PRD §15)

- **Audio is captured only while the hotkey is held** and kept in RAM for that query. It is
  transcribed on device by default and never stored, except the recordings you add to the
  replay set (PRD §15.1). The scripted session is that addition, made on purpose, as one
  decision for the set; real-use recordings are added one at a time.
- **Each decision is a line in `consent.jsonl`:** time (UTC), what it covers (the scripted set
  for a script hash, or one real-use file) and its scope. Never edited; a withdrawal is a new
  line, after which the files go. Scopes:
  - `replay-on-device`: replay through on-device engines only;
  - `replay-deepgram`: may also be streamed to Deepgram in the bake-off (engine D). That audio
    leaves the machine, with `mip_opt_out=true`, to a provider whose retention is "only for the
    duration necessary" (PRD §15.2). It is asked separately, and never before the owner has
    approved the first paid Deepgram call (PRD §17).
- **Retention:** until M5 + 12 months, so README numbers stay traceable (PRD §15.2); then the
  files are deleted.
- **Traces** of replay runs hold timings and IDs, never transcript text (PRD §10.5).

## Split discipline

- Develop and debug on the dev rows. Look at test-row results only in measured runs, and record
  every measured run.
- **Test rows already looked at:** t036, t074 and t075 prompted the rule questions above. Any
  test number after a parser change about fuzzy matches, leftover words or back-references is a
  post-change number and says so.
  - The pre-change text-level number is 94 of 100.
  - Such a change is built on the dev rows d01, d02 and d06, and fixes the rule, never the one
    utterance (CLAUDE.md: never special-case test inputs).
- Changing the script after any recording starts a new script hash and a new recording session.
  Fixing a reading only changes the expected columns; say so in the commit.
