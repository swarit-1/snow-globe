# Bench: the replay scorer and its report

**Updated:** 2026-11-23 (UTC).

**Nothing here has scored a real trial.** The harness that plays the recordings (ticket M1-09)
and the recordings themselves (M1-10) do not exist yet. This folder is the side of the harness
that needs neither: it reads replay traces, re-parses what the speech engine heard, and writes the
replay report of PRD §10.6, as PLAN.md M1 1c and its gate evidence ask. It has run only on
synthetic runs that [synthetic.py](synthetic.py) makes up, and every report of one says so at its
top, before any number.

## What is here

| File | What |
|---|---|
| [score.py](score.py) | The scorer and its command: joins each trace line to its script row, transcript and sidecar; re-parses; measures; groups; bootstraps; writes the report |
| [report.py](report.py) | Renders the report's JSON as Markdown, adding no number of its own |
| [traces.py](traces.py) | Reads trace schema 2 strictly, and the transcripts, sidecars and run manifest |
| [wer.py](wer.py) | Raw and normalised WER, with the number and place normaliser (bench/README.md "Scoring") |
| [stats.py](stats.py) | Type-7 percentiles, the Wilson interval, the seeded cluster bootstrap |
| [synthetic.py](synthetic.py) | Makes up a run (traces, transcripts, sidecars, run.json) to test and demonstrate the scorer |
| [trace_example.py](trace_example.py) | Reads the schema-2 example that `app/core/tests/trace_fields.rs` builds by hand on a fake clock and checks against the `SCHEMA_TWO_EXAMPLE` string in the same file. The tests share that one copy. It is a schema example kept inside the Rust test, not a trace file and not a measurement |
| `test_*.py` | Tests (stdlib `unittest`) |

Everything here is Python standard library only, run on workers/common's Python 3.13 as
`check.py` is. Nothing here reads or writes audio; only the `.json` sidecars beside recordings
are opened.

## Running it

```sh
# a made-up run, in a folder outside the repository (synthetic.py refuses the repository, the
# data dir's fixtures folder, and any folder holding a run or sidecars it did not make)
python3 -I -B bench/replay/synthetic.py --out /tmp/replay-synthetic

# score it (needs snow-globe-parse; see ../utterances/check.py for the build command)
python3 -I -B bench/replay/score.py --traces /tmp/replay-synthetic/traces.jsonl \
    --transcripts /tmp/replay-synthetic/transcripts.jsonl \
    --fixtures /tmp/replay-synthetic/fixtures --run /tmp/replay-synthetic/run.json \
    --out /tmp/replay-synthetic/report

python3 -I -B -m unittest discover -s bench/replay -t bench/replay -p 'test_*.py'
```

- `score.py` refuses fewer than 10,000 resamples, an `--out` inside the repository, and a
  parser binary older than its sources (exit 2, as `check.py`; `--allow-stale-binary` overrides,
  and the report then names the newer sources). For a measured run it also refuses a parser
  other than the one the run manifest names (exit 2; `--allow-other-parser` overrides, and the
  report says so at its top).
- **A stage that fails is never a verdict.** If snow-globe-parse cannot be run on a transcript,
  exits with a status other than 0 or 2, times out or prints something other than its JSON, or
  the WER normaliser fails, that trial is left out of the figures the stage feeds. The report
  says how many at its top, and `score.py` exits 1 after writing it.
- It bootstraps the groups in parallel (`--jobs`, default one fewer than the cores). Each group's
  seed comes from `--seed` and the group's name, so the result does not depend on `--jobs`.
- It writes `replay-report.json` (everything, per trial too) and `replay-report.md`. Neither holds
  transcript text: a trial's record has ids, verdicts and numbers only. The JSON never holds a
  bare `NaN`: a number that is not finite is refused on the way in and on the way out.
- The end-to-end tests in `test_synthetic.py` run the real binary, and are skipped (never faked)
  when it is missing or stale, as `check.py`'s live tests are. The other tests feed the re-parse
  outputs recorded from the binary, for dev rows only, or made-up stand-ins for the binary that
  fail on purpose.

## Inputs

**Traces.** JSON lines in trace schema 2 ([app/core/src/trace.rs](../../app/core/src/trace.rs)).
A line is a trial when it has a `replay` block; every other line is counted and skipped. Every
line is checked strictly: a missing or unknown key, a wrong type, an unknown enum name or a number
that is not finite stops the run with the line and key named. Schema 2 adds these fields to PRD
§10.5's list:

| Field | What | Why |
|---|---|---|
| `context` | The last place the parser was given, as manifest ids (`{"kind": "place", ...}`), or `empty`, `not_recorded`, `unlisted` | The scorer re-parses at it (bench/README.md "Scoring"); t100 must show `empty` |
| `state.display` | `on`, `asleep` or `unknown` when the query began | D-068: display-asleep trials are reported apart and inside the totals |
| `speech` | Engine, silence pad (ms), model retention, and whether a launch-time warm-up ran | D-067: the pad is logged with every trial, and the bake-off measures it with retention and warm-up |
| `levers` | Tier 1 pre-render from a stable partial, Tier 0 paint from a stable partial, and the harness's paint capture: each `true`, `false` or `null` (not recorded) | PRD §7.2 (pre-render on vs off), §7.1 lever 2 and §14 (partial paint), §10.4 (capture on vs off) |
| `marks.audio_first_sample` | Capture time of the session's first audio sample | A fixture's speech onset is counted from it |
| spans `partial_lag`, `stable_lag` | One per PARTIAL or STABLE event: from the end of the audio it covers to its arrival | PRD §10.6 "partial lag" |
| `paints` | Each composited paint: tier, source (`partial` or `final`), scene index, `matches_final`, time | PRD §7.1 lever 2 and §10.3: wrong paints, visible-wrong time, flips |
| `replay` | Script hash, utterance id, take, harness session, trial number | Joins the line to its row, sidecar and transcript, and orders a session's trials |

Every string a trace holds is an enum name, a timestamp, a manifest id (checked against the place
table when it is recorded) or a replay id of a fixed form; a test in `trace.rs` walks the JSON to
prove it. Trials are pooled only within one **setup**: the speech setup and the levers together.

**Transcripts** (`TODO(M1)`: the harness writes them from the app's final transcripts):
`transcripts.jsonl`, one line per trial, `{"session": int, "trial": int, "final": str}`, with an
optional `utterance` that must agree with the trace. The trace's lengths must agree with the
text, or the trial is flagged. A transcript whose session and trial no trace line names is
counted in the report: that trial left no trace, so it is missing from every denominator.

**Sidecars** (`TODO(M1)`: the recording tool writes them):
`<fixtures>/scripted/<script hash>/<id>.<take>.json` with `id`, `take`, `script_hash`,
`key_up_ms`, `speech_onset_ms` and `speech_end_ms`, offsets in ms from the recording's first
sample. The replay puts that sample at `audio_first_sample`. If the trace's key-up is more than
50 ms from where the sidecar puts it (`KEY_UP_TOLERANCE_MS`, proposed; `TODO(M1)`: set it from
the harness's measured buffer period and key-injection jitter), the trial is flagged and its
first partial after onset is left out.

**Run manifest** (`TODO(M1)`: the harness writes it): `run.json` with `"kind": "measured"` or
`"synthetic"`. A measured run also names its frozen build, `build_commit` (git commit, hex) and
`parser_sha256` (the SHA-256 of that build's snow-globe-parse); the scorer refuses a measured
manifest without them. An optional `tier2_enabled` (true or false) says whether the run sent
Tier 2 at all. A report says "measured" only when the manifest says so and no transcript or
sidecar is marked synthetic; with no manifest its provenance is "unknown".

## What it computes

Per trial, the re-parse runs `snow-globe-parse` on the final transcript at the row's reference
clock and at the trace's `context`, and `check.py`'s `compare()` judges the result against the
row's expected columns. Then, per **setup × split × state × display** (and the idle follow-ups,
D-068), every metric below gets a point estimate and a 95 % interval. The headline is idle trials
on the test split, for the latency budgets (PRD §10.1 "p95, idle"). The accuracy targets (exact
place and time, the fallback rate, wrong paints) are shown against the whole test split, every
state, as PRD §14 and §7.1 set them, with idle beside. The dev split is reported separately and
is not for claims.

**An empty final transcript** is wrong on place and time and is not re-parsed: the parser would
resolve an empty text to the context place now, which is the expected answer of rows such as d16.
It counts in the exact and place figures, and not in the fallback rate. The report also flags a
trial whose app outcome contradicts its transcript or its re-parse: an empty transcript with an
outcome that painted, an `empty` outcome with words, or the app finding a place, no place or the
fallback where the re-parse finds another of the three.

**Latencies** (ms; p50 and p95 with intervals):
- every stage between two marks: key held, first partial after key-down, place matched and assets
  ready after key-down, assets ready after key-up (negative when before it), release → final,
  parse, forecast after parse, and the four Tier 2 stages;
- **a tier's paint time** is its earliest recorded paint, never a paint mark alone: a mark with no
  paint behind it is a webview or IPC time (PRD §10.4: diagnostics, never the paint time), so it
  is left out and flagged;
- **Tier N:** key-up → the first paint of tier N whose scene is the final parse's. A wrong paint
  never counts (PRD §10.3); key-up → the first Tier 0 paint, right or wrong, is reported beside
  it. A Tier 0 painted right from a stable partial before key-up is negative;
- **first partial after speech onset** (PRD §14): `speech_first_partial` − (`audio_first_sample`
  + the sidecar's `speech_onset_ms`);
- **partial lag** and **stable lag**: pooled over events, each event weighing one;
- **visible-wrong time**: from a wrong paint to the next paint of another scene;
- every other span, per occurrence.

**Shares** (Σ numerator / Σ denominator, each denominator named in the report):

| Metric | Counted in the denominator |
|---|---|
| Exact place and time (D-057's figure) | trials of rows not tagged `owner-pending` |
| … on settled rows | the same, less the rows whose stored text-level agreement is `no` (today d06 and t074), which are reported apart until the lead resolves them |
| … on owner-pending rows | trials of the `owner-pending` rows, apart |
| Exact or soft | as D-057's figure; `soft` is a rolled flag, or a miss against the fallback |
| Place right | as each exact figure. A resolved row needs its viewpoint (either one of a close call); a miss must resolve to no place; a fallback row must reach the fallback or its viewpoint |
| Fallback | every re-parsed trial (an empty transcript is not re-parsed) |
| WER, raw and normalised | reference words: Σ(S + D + I) / ΣN. Normalised N is the reference's words with numbers spelt out, never one token per place (below) |
| Wrong paints from partials, per partial paint (PRD §14's figure, proposed) | trials that painted from a partial whose verdict is known |
| Wrong paints from partials, per judged trial | trials not superseded before their final parse |
| Wrong paints from the final | trials that painted (expected 0: it would be a bug) |
| Flips per query | trials that painted; a flip is a paint whose scene index differs from the paint before it |
| Utterances still in speech at key-up (PRD §10.6) | each take played in the group, counted once however often it was replayed: `speech_end_ms` > `key_up_ms`. A per-trial figure is in the JSON beside it, labelled |
| Context differed | follow-up trials whose context was recorded |
| Hit rates (Tier 0 ≤ 300 ms, Tier 1 ≤ 1 s and ≤ 400 ms, Tier 2 ≤ 3 s, release → final ≤ 240 ms, first partial ≤ 300 ms after onset) | trials due a paint (painted, errored, or with a paint matching the final) for the tiers; trials with the value for the rest. Tier 2's is left out of a group that never tried Tier 2: the run manifest's `tier2_enabled` decides, else any Tier 2 mark, span (readback, encode, proxy, provider) or paint in the group's traces |

**Normalised WER** ([wer.py](wer.py)) spells numbers as `check.py`'s `spoken()` does, joins
"a.m." forms, splits hyphens and expands "Mt" and "Pt" on both sides. Then a place said in any
manifest alias of a place the reference names may be scored as the reference's own words for that
place, whichever reading gives fewer errors. So "Mount Rainer", "Rainier" and "Tahoma" against
"Mount Rainier" are no error, another place is still an error, a single wrong word elsewhere
weighs what it weighs raw (on every script line N is at least the raw count), and an empty
transcript scores 100 %.

**A follow-up** whose live context is not the script's `context` place is wrong on place, whatever
the re-parse gives (bench/README.md "Scoring"), and the report says why:
`row-before-not-played` (the trial just before it in its session was another row, or another
setup), `row-before-unscorable` (it was the row before, but its place could not be judged: no
transcript, no recorded context, or a stage failed), `row-before-misheard` (the row before was
played, and its re-parse did not resolve to the place, or it heard nothing) or `app-lost-it` (it
did, yet the app's context was another). A `no-place` row such as t100 played with a non-empty
context is flagged.

**Data checks**, counted in the report: an idle label with under 30 minutes idle, a warm label
with 60 s or more, a transcript whose lengths are not the trace's, a trial superseded by a newer
key press, an empty final transcript, an app outcome that contradicts the transcript or the
re-parse, a key-up away from the sidecar's, a paint mark with no paint, and transcripts with no
trace line. Every report also states the replay set's known bias: one speaker, one microphone,
one room (PRD §10.6).

## Statistics

- **Cluster bootstrap** ([stats.py](stats.py)): utterances are resampled with replacement, each
  bringing all its trials, because repeated plays of one recording are correlated (PRD §10.6).
  When a group spans two or more harness sessions, sessions are resampled too, independently,
  each trial weighted by the product of its utterance's and its session's counts (the pigeonhole
  bootstrap for crossed factors, Owen 2007). The share of utterances still in speech at key-up is
  a property of each recording, so it is resampled by utterance over the takes played, never by
  session. At least 10,000 resamples; 95 % percentile intervals; a resample with no value for a
  metric is left out and counted (`valid_resamples`).
- **Wilson** for the wrong-paint shares: the score interval without continuity correction. The
  report gives the two-sided 95 % interval and the one-sided 95 % upper bound, and puts Wilson
  beside the ≤ 2 % target (never the bootstrap interval, which is 0 to 0 when no paint was
  wrong); which bound PRD §14's "≤ 2 % (Wilson upper bound)" means is for the lead. With no wrong
  paint at all, the one-sided bound first falls under 2 % at 133 trials and the two-sided at 189
  (computed with stats.wilson). Wilson treats trials as independent, so the bootstrap interval is
  given in the wrong-paints section, labelled.
- **Percentiles** are type 7 (numpy's default) everywhere. A p95 from about a hundred trials rests
  on its top five or so, and its interval says so.
- **Known answers** pin each piece: Newcombe's (1998) Wilson table; exact bootstrap distributions
  for two clusters and for a 2 × 2 crossed design; the bootstrap variance of a mean; brute-force
  edit distances.

## The synthetic run

[synthetic.py](synthetic.py) makes up 650 trials: two engine setups (`dictation_transcriber` in
three sessions, `parakeet` in two, as labels only), every state, the display asleep for most idle
trials, paints from partials with some wrong, partial lags, and one each of an app that lost a
follow-up's context, a row before that was misheard, an empty transcript and a mislabelled idle
trial. Every file says it is synthetic, and so does every report made from it. Its `--out` may be
new, empty or an earlier synthetic run; a folder holding a run or sidecars it did not make is
refused, so a real run is never overwritten.

**Split discipline** (bench/README.md): every test row is played exactly as `check.py` parses
it, its own script text at the script's context column (empty unless it is a follow-up). So its
re-parse is the very run whose output scripted.tsv stores, scoring the run shows nothing about the
parser on the test rows that `check.py` has not already shown, and its test-split WER is 0 by
construction. The app's running last place, every mishearing and every context anomaly are on dev
rows. `test_synthetic.py` checks every test row's text and context, and that every test row then
scores exactly as scripted.tsv stores it. A trace's outcome follows the parse recorded for the
row's own text, so the report may flag misheard dev rows whose re-parse disagrees with it.

## Not done yet

- `TODO(M1)`: the harness, which writes the traces' `replay` block, the paint and key stamps
  (ScreenCaptureKit and its listen-only tap, PRD §10.4), the speech setup and levers,
  `transcripts.jsonl` and `run.json` (with `build_commit`, `parser_sha256` and `tier2_enabled`),
  and runs this scorer as its one command (PRD §10.6).
- `TODO(M1)`, for the lead: PRD §10.4's pixel tag carries (query_gen, tier) only, so when Tier 0
  paints the partial's scene and then the final's in one query, the captured frames cannot say
  which composite showed which scene. The headline "Tier 0 matching the final" needs exactly that.
  Either the tag also carries the scene index (or a per-query paint sequence), a PRD §10.4 change,
  or the webview's paint gate logs each composite's scene for the harness to join to the frames.
- `TODO(M1)`: the recording tool, which writes the sidecars.
- `TODO(M1)`: `KEY_UP_TOLERANCE_MS` from the harness's measured buffer period and jitter.
- `TODO(M1)`: real-use utterances (PRD §10.6, reported separately). Their trace lines are read and
  rejected with a reason; scoring them needs their confirmed reference transcripts.
- `TODO(M1)`: Tier 0 "run and measured but not painted" when Tier 1 is ready within ~100 ms
  (PLAN.md M1 1b). The trace has no mark for a Tier 0 that was ready but skipped.
