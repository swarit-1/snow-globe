# Design-H archive backfill planner

Offline research tooling behind [docs/research/archive-backfill-plan.md](../../../docs/research/archive-backfill-plan.md). It is not part of the `lookout-eval` package, and `make test` does not collect it (eval's `testpaths` is `tests/`). Eval's `ruff check .` and `ruff format --check .` do cover it.

## Files

| File | What it does |
|---|---|
| `plan.py` | Builds the tranches (T0, T1a, T1b, T3pr, T2, T1c), their request, byte and HRRR estimates, and the queue scripts (`first-tranche.sh` and the `draft-*.sh` files) |
| `loop_sim.py` | The time model: capture's real `QueueRunner`, limiter, budget and storage on a fake clock against a mock nps.gov |
| `measure_inputs.py` | Re-measures the plan's inputs: asset and search sizes, request times and errors, from sidecars, cached searches and capture logs |
| `test_plan.py` | 14 tests: the held-out guard on every planned date, one-bin Sun lookups through eval's own extractor, and no local paths in the outputs |

## Safety

- **No network.** Each script disables sockets before anything else runs. They read only metadata under `~/lookout-data`: day masks, cached searches for dates before 1 Jun 2026, state files, sidecars and capture logs. They never open an image or read `quarantine/`.
- **Outputs.** They write only under the output folder you pass. `plan.py` refuses to write a queue file under `~/lookout-data`, and the scripts it generates refuse to run unless `Q` names the queue file.
- **Dates.** No planned date falls on or after 1 Jun 2026 (D-019). `make_entry` and the queue runner refuse one anyway (plan section 9).
- **Who runs the scripts.** Only the lead runs a generated script against the live queue (`Q=$HOME/lookout-data/state/archive/queue.json`). The running capture loop then works the queue (D-029).

## Running it

Run it from `eval/`. It needs capture's queue code and the shared library on the path:

```sh
cd eval
PYTHONPATH=src:../workers/common/src:../workers/capture/src \
    uv run --no-sync python -m pytest -q -p no:cacheprovider tools/backfill_plan/test_plan.py
PYTHONPATH=src:../workers/common/src:../workers/capture/src \
    uv run --no-sync python tools/backfill_plan/plan.py <scratch>/out
```

## When to run it again

Regenerate T1b, T3pr, T2 and T1c once T0 has dated the 3:2 to 16:9 switches (plan section 8, "After T0 finishes").

The first T0 frames, on 20 Nov 2026, put Point Reyes's switch between 27 Mar and 10 Apr 2023 (999×666, then 999×562). The plan had assumed about June 2023, so widen Point Reyes's 16:9 span in `plan.py` before generating the later tranches.
