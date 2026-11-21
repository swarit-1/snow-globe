"""``snow-globe-eval data-report``: the M0.5 gate's log evidence (PRD §16 M0.5; PLAN.md gate 1).

PRD §16 closes M0.5 on "≥ 72 h of worker and capture logs", and PLAN.md's gate asks for "runs
and frames per viewpoint, plus exclusions". This package builds that table from what the
running jobs leave under ``$SNOW_GLOBE_DATA_DIR``. It is offline and read-only: it reads
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
errors, the longest and the shortest gap between polls send to send (``sent_at`` from capture
0.2.1, else ``t − elapsed_ms``), the most image GETs sent in any rolling hour (PRD §15.3: at
most 4), and the live HRRR runs whose first write at that viewpoint falls in the window, with
their hours and fields. A later write of a run is a re-extraction, never a run; a run due
before the window and first written inside it is a catch-up, counted and listed apart.

**Polite collection at nps.gov** (PRD §15.3, per viewpoint): nps.gov received each request no
earlier than its send and no later than its answer, which the record's millisecond stamps
bound: the earliest receipt is ``sent_at``, else ``t − elapsed_ms`` less 0.05 ms (none when the
record has neither), and the latest is ``answered_at``, else ``t`` + 1 ms (``t`` is written
after the answer, truncated). So nps.gov saw two consecutive polls at least
``earliest(N+1) − latest(N)`` apart (``poll_gap_at_nps``), and 5 consecutive image GETs spread
over at least ``earliest(5th) − latest(1st)`` (``image_gets_at_nps``; 5 GETs within 3600 s
would break the cap of 4 in a rolling hour). Each gives the shortest bound and the *unproven*
pairs or windows: not provably 300 s (3600 s) apart, because the bound is under the limit or a
send cannot be placed. Unproven is not the same as shown closer. The send-to-send figures stay
beside them; the report's ``rules.bases`` names each figure's basis.

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
- *Starts and stops:* a change of capture release (``capture_version``, read the same under
  its old and new names, ``lookout-capture/<v>`` and ``snow-globe-capture/<v>``, D-080) or of
  runner between consecutive passes, a ``loop_stopped`` record, or two pass starts under 295 s
  apart (the 300 s interval less a 5 s tolerance; one loop process never starts passes that
  close) mark a capture restart. A restart's *excess* is the longest any camera went from its
  last poll before the restart to its first poll after (send to send), less one 300 s
  interval, when positive; it is lost from
  that camera's last poll + 300 s to its next poll. A poll is before the restart when its
  record was written by the old loop's last record: its ``loop_stopped`` record, else its last
  record (the old version and runner) before the new loop's first pass start. A change in the
  fields or hours of consecutive live runs marks the forecast stand-in restarting with new
  settings.

**The 72 h clock** (:mod:`.report`): covered = elapsed − lost, and the earliest close is the
moment covered reaches 72 h if nothing more is lost. The headline is the *gate rule*, the
lead's decision of 20 Nov that settles D-061 (PRD §16's "worker and capture logs", read
strictly): lost = the union of both jobs' outages and each capture restart's excess. Beside it,
labelled: PLAN.md's capture-only figure (the capture outages alone) and each job's own.
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
