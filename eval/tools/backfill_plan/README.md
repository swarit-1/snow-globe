# Design-H archive backfill planner

Offline research tooling behind [docs/research/archive-backfill-plan.md](../../../docs/research/archive-backfill-plan.md). It is not part of the `snow-globe-eval` package, and `make test` does not collect it (eval's `testpaths` is `tests/`). Eval's `ruff check .` and `ruff format --check .` do cover it.

## Files

| File | What it does |
|---|---|
| `plan.py` | Plans from the queue's current state: the queue as it stands (T0 and T1a), then T1b, T3pr, T2, T1c and TQ (the owner's question), with their request, byte, frame and HRRR estimates. Writes the draft queue scripts and two scratch queues |
| `loop_sim.py` | The time model: capture's real `QueueRunner`, limiter, budget and storage on a fake clock against a mock nps.gov. `--mode hours` measures the loop time one day's budget takes; `--mode days` works the queue UTC day after UTC day and gives each tranche's finish day. A tranche is finished only when none of its entries is open, failed or stopped; `--resume-failed` adds the lead's daily `archive queue resume --all` |
| `measure_inputs.py` | Re-measures the plan's inputs from sidecars, cached searches, capture logs and the queue: frame and search sizes, request times and errors, exact end-to-start spacing, passes, loop uptime per UTC day. `--day 2026-11-21.jsonl` measures a later day's queue requests too (`hours_to_1000th`, the time model's out-of-sample check) |
| `test_plan.py` | 44 tests: the original 14; the spans, the UTC reading of the held-out edge, no planned date at or past the edge, the T3pr shell check and planning from the queue's state (15); and one or more per review finding of 20 Nov (15), among them the drafts run by `/bin/sh` with a stub `uv` and a days-mode run on the real `QueueRunner` |

## Safety

- **No network.** `plan.py` and `loop_sim.py` replace every socket connect, datagram send and name lookup (`getaddrinfo`, `gethostbyname`, …) before anything else runs; `measure_inputs.py` opens none. They read only metadata under the data dir (`$SNOW_GLOBE_DATA_DIR`, else `~/lookout-data` until the rename's live switch, then `~/snow-globe-data`; D-080): day masks, cached searches for dates before 1 Jun 2026, state files, sidecars and capture logs. They never open an image or read `quarantine/`.
- **The queue is read, never written.** Pass a copy with `--queue`. `plan.py` refuses to write under `$SNOW_GLOBE_DATA_DIR`, and `loop_sim.py` refuses a queue file there. These are explicit checks (`refuse_under_data`), not `assert`s, so `python -O` cannot skip them.
- **Dates.** Every planned date passes both readings of the held-out edge (plan section 9): its local date, and the wall-clock date of the latest frame it can yield, are before 1 Jun 2026 (D-019, D-047), and that frame is before 1 Jun 2026 00:00 UTC, judged by instant (the lead's rule of 20 Nov until the owner confirms D-038). At all six cameras the last such date is 30 May 2026. The planner also stops if an open queue entry fails either reading.
- **Scripts.** Every generated script refuses to run unless `Q` names an existing, non-empty queue file by an absolute path (a mistyped path would start a new queue the loop never works). `draft-T3pr.sh` refuses, before queuing anything, an `EVENINGS` list that is empty or names a day that is not one of the plan's Point Reyes T1 days (`T3PR_DAYS` in the script: T1a as queued, T1b as planned) or is after 30 May 2026; `set -f` keeps the list from being expanded as file names. `draft-TQ.sh` is the owner's question: it refuses to run unless `TQ_CAMERAS` names the approved cameras, and queues only their blocks. Only the lead runs the drafts against the live queue (`Q=$HOME/lookout-data/state/archive/queue.json`, valid before and after the rename's live switch, which leaves `~/lookout-data` as a symlink to `~/snow-globe-data`; D-080); the running capture loop then works the queue (D-029). The planner never runs them.

## Running it

Run it from `eval/`. It needs capture's queue code and the shared library on the path:

```sh
cd eval
PYTHONPATH=src:../workers/common/src:../workers/capture/src \
    uv run --no-sync python -m pytest -q -p no:cacheprovider tools/backfill_plan/test_plan.py
cp ~/lookout-data/state/archive/queue.json <scratch>/queue.json         # read a copy (the live queue)
PYTHONPATH=src:../workers/common/src:../workers/capture/src \
    uv run --no-sync python tools/backfill_plan/plan.py <scratch>/out --queue <scratch>/queue.json
PYTHONPATH=src:../workers/common/src:../workers/capture/src \
    uv run --no-sync python tools/backfill_plan/loop_sim.py <scratch>/sim --queue <scratch>/out/queue-plan.json \
    --plan <scratch>/out/plan.json --tranches T1a,T1b,T3pr --mode days --runs 4 [--resume-failed]
uv run --no-sync python tools/backfill_plan/measure_inputs.py --queue <scratch>/queue.json [--day 2026-11-21.jsonl]
```

`plan.py` takes `--today YYYY-MM-DD` (UTC; default today) to read that day's budget file, so the schedule starts on the first UTC day with budget left. `loop_sim.py --logs DIR` reads copied capture logs. The tests run in about 10 s; `make test` does not collect them (eval's `testpaths` is `tests/`), so run them as above after any change to the planner or to capture's queue code.

`plan.py` writes, under the output folder: `plan.json` (every entry, queued or planned, with its tranche, status and expected cost), `analysis.json` (with PLAN.md's target and the measured miss of each tranche, T3pr's costs in both orders, and the schedule with TQ's Point Reyes block ahead of T2), `hrrr-valid-hours.json`, `draft-T1b.sh`, `draft-T3pr.sh`, `draft-T2.sh`, `draft-T1c.sh`, `draft-TQ.sh`, `ids-T1b.txt`, and two scratch queues for the time model: `queue-plan.json` (the queue, then T1b, T3pr, T2, T1c) and `queue-plan-t3pr-first.json` (T3pr before T1b).

## When to run it again

- After the lead queues a tranche, or the owner answers a TQ question: the planner reads the queue, so entries already queued keep their place and are not planned again. T3pr's queued nights and hand-chosen clear days count as T3pr: a season whose night is queued gets no second night, and each queued clear day takes the place of one cost-model day.
- When the owner confirms D-038's local-day reading: the UTC reading goes, and 31 May 2026 becomes plannable again (`plannable_in`).
- When eval's registration reports on Big Bend's 2024 and Grand Canyon's winter 2024/25 view checks: change `SPANS[BIBE].design_start` and `HOLDS[GRCA]`.
