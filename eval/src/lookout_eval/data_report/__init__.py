"""``lookout-eval data-report``: the M0.5 gate's log evidence (PRD §16 M0.5; PLAN.md gate 1).

PRD §16 closes M0.5 on "≥ 72 h of worker and capture logs", and PLAN.md's gate asks for "runs
and frames per viewpoint, plus exclusions". This package builds that table from what the
running jobs leave under ``$LOOKOUT_DATA_DIR``. It is offline and read-only: it reads
``capture_log/``, ``forecast_log/`` (with ``forecast_log/lag_survey/`` when present), the JSON
sidecars under ``frames/`` and ``thumbs/``, ``forecast/hrrr/`` and ``state/``. It never opens
an image, never reads ``archive/`` or ``quarantine/`` (D-019, D-022; every path it reads also
passes eval's quarantine guard), never writes a file and never takes a lock. Every string it
outputs shows the home directory as ``~``, so the evidence carries no account name.

**Window.** From ``since`` (default 12:24:23 UTC on 19 Nov 2026, when the capture loop took
over all 8 cameras, D-023) to ``until`` (default: now), half-open, on each record's ``t``.

**What counts.** A capture record counts when it is a ``worker: capture`` record of a scheduled
runner (``cli-loop`` on this Mac, ``lambda`` on AWS) inside the window. A forecast ``run``
record counts when it is live (``"live": true``), inside the window and the first write of at
least one viewpoint; a ``lag`` counts once per run and hour (the first kept), and an ``error``
counts. Everything else is an exclusion, listed with its reason: records outside the window,
archive events (``worker: archive``), other runners (``cli-once``, the retired stopgap's
records), archived forecast runs (``"live": false``, such as the backtest pair's run
2024011412), re-extractions of a run already written (inside the window or before it),
duplicate lags, survey lags and damaged log lines.

**Per viewpoint** (:mod:`.capture`, :mod:`.forecast`): polls (``request`` records of the poll
stage: the JSON GET at the ARD cameras, the HEAD at Rainier), image GETs, downloads stored and
what each storage policy kept (full-size frames and thumbnails, PRD §8.7, D-028), rate caps,
errors, the longest gap between polls, the shortest gap between polls on send times
(``sent_at`` from capture 0.2.1, else ``t − elapsed_ms``), the most image GETs in any rolling
hour (PRD §15.3: at most 4), and the
live HRRR runs whose first write at that viewpoint falls in the window, with their hours and
fields. A later write of a run is a re-extraction, never a run; a run due before the window and
first written inside it is a catch-up, counted and listed apart.

**Outages** (PLAN.md: they extend the 72 h clock, as they extend the sealed window):

- *Capture:* a gap of more than 540 s between consecutive pass records of the scheduled
  runners. The loop starts a pass every 300–330 s, so the time lost is the gap minus one 300 s
  interval (the reading behind PLAN.md's 8.2 h for 19 Nov). A last pass more than 540 s before
  ``until`` is an ongoing outage, and no scheduled pass in the logs loses the whole window.
- *Forecast:* the stand-in leaves records only when it writes a run (``run`` and ``lag``) or
  fails (``error``), about once an hour, so a quiet gap between its records is normal; gaps
  over 900 s are listed for information only. Instead each HRRR cycle is due when the hours it
  extracts were all on S3 (the run record's ``s3_lags_s``), and a run is late when its first
  write *finished* (``fetch_finished_utc``, D-027) more than 900 s after that. The stand-in was
  down from due + one 300 s tick until it next finished writing a live run, any run; a fetch's
  start or an ``error`` record does not end that. The span is an outage when it is longer than
  900 s. A cycle never written gets an estimated due time (the cycle + the median measured lag:
  of the window's complete runs, else of every complete run read, else D-027's conservative
  120 min) and counts the same way, flagged as estimated. Cycles due up to 6 h before
  ``since`` lose the part of their span inside the window, and no live run in the logs loses
  the whole window. These are lower bounds: there is no per-tick record.
- *Starts and stops:* a change of ``capture_version`` or runner between consecutive passes, a
  ``loop_stopped`` record, or two pass starts under 295 s apart (the 300 s interval less a 5 s
  tolerance; one loop process never starts passes that close) mark a capture restart. A change
  in the fields or hours of consecutive live runs marks the forecast stand-in restarting with
  new settings.

**The 72 h clock** (:mod:`.report`): as PLAN.md states it, covered = elapsed − the capture
loop's lost time, and the earliest close is the moment covered reaches 72 h if nothing more is
lost. Beside it the report gives a *proposed* both-jobs figure (lost = the union of the capture
and forecast outages, for PRD §16's "worker and capture logs"), which no planning doc records
yet, and each job's own figure.
"""

from __future__ import annotations

from .common import DEFAULT_SINCE, LEGACY_POLICY, REPORT_KIND, SCHEDULED_RUNNERS, ReportOptions
from .report import build_report
from .table import format_report

__all__ = [
    "DEFAULT_SINCE",
    "LEGACY_POLICY",
    "REPORT_KIND",
    "SCHEDULED_RUNNERS",
    "ReportOptions",
    "build_report",
    "format_report",
]
