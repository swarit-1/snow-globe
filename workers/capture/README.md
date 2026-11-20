# lookout-capture

Polite capture of the 8 NPS cameras in `viewpoints/manifests/` (PRD §8.6, §11.2, §15.3), PRD §8.7
storage (thumbnails, and the frame nearest each hour), the NPS AirWebCams archive backfill (also
worked inside the live loop), the M0.5 archive check (PRD §13.2, §13.9), and two no-network
measurements: a camera's update cadence and the bytes stored per day. Python 3.13, uv.

```sh
cd workers/capture && uv sync && uv run pytest -q && uv run ruff check .
# On this laptop iCloud hides the venv's .pth files; run with the sources on the path instead:
PYTHONPATH=src:../common/src .venv/bin/python -m pytest -q
```

## Commands

| Command | Network | What it does |
|---|---|---|
| `lookout-capture once [--only <id>]` | NPS live | One pass over the cameras that are due. Prints a JSON summary. |
| `lookout-capture loop [--interval 300] [--jitter 30] [--archive-queue PATH] [--archive-budget 1000] [--archive-total-gb 5]` | NPS live (+ archive) | A pass every interval plus 0–jitter s. With `--archive-queue`, queued archive work runs between passes in the same process (below). The first Ctrl-C finishes the current camera and exits; a second interrupts at once. |
| `lookout-capture status [--json] [--archive-queue PATH]` | none | Persisted state per camera (the frame policy the newest frame's writer used, marked `*` where this code would use another; frames staged; last hourly decision; store failures; thumbnails pending; the next poll and what it is counted from, `next_poll_basis`: a recorded answer, an answer bound, a reservation, or state from older code, where the next poll shown is what this code would do if it took over now), today's event counts, and the archive queue's progress. |
| `lookout-capture archive queue add <id> <YYYY-MM-DD>... [--through YYYY-MM-DD] [--hires] [--thumbs] [--pick all\|hourly\|midday] [--daytime-only] [--min-sun-deg X] [--max-gb 1] [--queue PATH]` | none | Queue days to backfill (`--through`: every day of a range, at most 400). A day on or after 1 Jun 2026 is refused (D-019). |
| `lookout-capture archive queue list [--json]` / `remove <id>... [--finished] [--all]` / `resume <id>... [--all] [--max-gb G]` | none | Show or edit the queue (default `state/archive/queue.json`). `--finished` takes done, refused, failed and stopped entries; `resume` sets stopped (size cap) or failed entries back to work, `--max-gb` raising their cap. Safe while the loop runs. |
| `lookout-capture cadence <id> [--hours 24] [--max-gap S] [--json]` | none | The camera's update cadence from `capture_log` and sidecars (below). |
| `lookout-capture storage [--day YYYY-MM-DD] [--only <id>] [--json]` (alias `storage-report`) | none | Bytes per frame and thumbnail per camera from disk, and bytes per day under PRD §8.7 against the old policy. Writes nothing. |
| `lookout-capture archive days <id>` | NPS archive | `GetAvailableDays`: first and last day, days per year. |
| `lookout-capture archive day <id> <YYYY-MM-DD> [--daytime-only] [--hires] [--dry-run] [--max-gb 1] [--held-out-override REASON]` | NPS archive | One day's frames (thumbmedium unless `--hires`). |
| `lookout-capture archive sample <id> <YYYY-MM-DD>... [--pick midday\|hourly\|all] [--held-out-override REASON]` | NPS archive | Picked frames of several days. |
| `lookout-capture archive-check [--viewpoint <id>]... [--rainier-page] [--budget 220]` | NPS archive | The M0.5 check; writes `archive/archive_check_<date>.json`. |

**One NPS fetcher per machine** (CLAUDE.md, D-021, D-023). Every command that contacts nps.gov runs inside a
host-wide lock, `$LOOKOUT_DATA_DIR/state/locks/nps-fetcher.lock` (`flock`, always on local disk,
also with `LOOKOUT_STORAGE=s3`). It exits with **status 3**, before sending anything, when:
- another `lookout-capture` fetcher holds the lock (the message names its pid and command);
- a Rainier stopgap process (`stopgap_rainier.py`) is running;
- stopgap-shaped records (`"vp"`, no `"worker"`) are in the local `capture_log` from the last
  11 minutes. After the stopgap is stopped, wait out that window (the message gives the time).

`loop` repeats the stopgap check before every pass and stops (status 3) if one appears. `status`,
`archive queue ...`, `cadence` and `storage` never contact nps.gov and never take the lock, so they
work beside a running loop.

**Archive backfill inside the loop** (D-023, `archive_queue.py`). `loop --archive-queue PATH` works the
queue between live passes, in the same process and so under the same lock, never as a second fetcher:
- A queue entry is one day at one viewpoint with options (`hires` and/or `thumbmedium`; `all`,
  `hourly` or `midday`; a minimum NPS Sun elevation). Working it is that day's one `Search` (or the
  cached one), then one asset GET per step.
- **One request at a time, ≥ 5 s apart:** the `nps-archive` limiter, as for every archive request.
  The queue starts no sooner than 1 s (the live request gap) after the pass's last live request,
  so live and archive requests of the loop are ≥ 1 s apart at the hand-over too.
- **Never delaying a due live poll:** a request starts only if the limiter's wait + 30 s (a 20 s body
  deadline plus the 10 s read timeout) + 5 s fit before the next pass is due. A step sends one
  request; a day's search that spans several pages (a day has at most 96 frames, under the 300 page
  size, so in practice never) sends each further page only if it fits too, and a search cut short
  is not used: it is searched again in a later gap, and given up after 3 tries.
- **One back-off for www.nps.gov** (`host_backoff.py`): an HTTP 429/503 from a live poll *or* an
  archive request writes `state/capture/hosts/www.nps.gov.json` (the larger of `Retry-After` and
  the back-off), and while it holds no archive step starts and no live camera is polled. A
  standalone `archive` command waits it out too.
- **The existing rules:** the held-out guard (queued days past it are refused when added and when
  worked; every asset is checked again); a request budget per UTC day (`--archive-budget`, default
  1000, persisted as `state/archive/budget-archive-queue-<date>.json`); the per-viewpoint size cap
  (`--max-gb`) and a **total cap** over every viewpoint's backfill (`--archive-total-gb`, default 5 GB,
  summed from `state/archive/<id>.json`; PRD §15.3); resumable (the queue file keeps each entry's
  position, `state/archive/<id>.json` what is stored). Failed downloads are retried in up to 3 sweeps.
- The queue file is edited under an `flock` on `<file>.lock` and replaced atomically. A finished entry
  keeps only counts (`selected_count`, `outcome_counts`), so the file stays small (under 1 KB an
  entry). `status` shows each entry's progress and the runner's last step.

**Held-out guard** (D-019, D-022, D-038, D-047, `heldout.py`). Archived frames, full size or
thumbnail, dated on or after **1 Jun 2026** are never requested or read. An asset is held out if
**any reading of its date, local or UTC, is on or after 1 Jun 2026** (the lead's reading of 20 Nov
2026, pending the owner's D-038; `asset_dates`):
- **local readings** (`asset_local_dates`): the date of `ImageCreateDateTime`, the date of
  `LocalTimeString`, the civil date of the parsed UTC time in the manifest zone, and the civil date
  of `ImageCreateDateTime` read as local standard time (in summer that field stays on standard
  time, so "05/31/2026 11:30:00 PM" with no readable `LocalTimeString` is 00:30 PDT on 1 Jun);
- **UTC readings** (`asset_utc_dates`): the UTC date of the asset's instant under both of
  lookout-common's conventions (`label`, the default, and `wall`: the instant behind its archive
  key either way), and the UTC date of each local reading converted in the camera's zone
  (`ImageCreateDateTime` as civil time, both sides of a DST fold, and as standard time;
  `LocalTimeString`'s clock as civil time). A reading with a date but no readable clock counts every
  UTC date its local day touches (fail closed);
- the instant behind the key itself, local and UTC, when the backfill has it (`instant=`).

So 31 May now turns at **UTC midnight**: at Point Reyes every frame from 16:00 standard (17:00 PDT)
is refused, 32 of the day's 96; at Acadia from 19:00 EST, 20; at Grand Canyon (Arizona keeps MST)
from 17:00, 28. On 20 Nov the lead removed the six 31 May queue entries; the owner may re-allow
them by confirming D-038. An asset with no readable date is refused. lookout-eval's
`heldout.nps_dates` reads exactly the same set: eval's fuzz loads this file and checks equal
dates, local and UTC, and equal decisions on 6,000 random cases, and both test suites carry the
same edge table per zone. `archive day|sample` refuses days on or after 1 Jun outright;
`--dry-run` reads metadata only and lists the refusals. Only scoring may pass
`--held-out-override "<reason>"`; the reason is logged (`held_out_override`) with every frame it
releases and written into the sidecar.

## Rules the code enforces

**Live** (`live.py`, state in `state/capture/<id>.json` under the lock `state/locks/capture/<id>.lock`):
- **Polling:**
  - ARD air cams: a conditional GET of the JSON feed.
  - Rainier park camera: a HEAD on the image.
  - **At most once per `poll_interval_s`, never under 300 s, counted from the last poll's answer**
    (the lead's decision of 20 Nov 2026, amending D-023 and D-048): the next poll may be sent only
    once the clock has reached the previous poll's **answer time** + 300 s + `POLL_MARGIN_S` (2 ms).
    The answer time is one clock reading, taken right after the response has been read in full,
    or right after the request failed or timed out; it is written rounded up to the millisecond
    (`last_answer_at` in the state, `answered_at` in the request record), the send stamp rounded
    down (`last_poll_at`, `sent_at`). See "Why the rules hold at nps.gov" below.
  - **A poll is reserved and saved before its send** (`poll_reserved_at`), as an image GET is, so
    a process killed between the send and the state save still leaves it counted (see "Upgrade and
    crash bounds").
  - **A request that failed before any answer came back counts as answered 300 s after the
    failure** (`last_answer_bound`/`answer_bound: failed_after_send`; below).
  - **No redirect is followed** (live and archive clients): httpx would send each hop as one more
    request that neither `capture_log` nor the caps see. A 3xx is an error like a 5xx, logged with
    its `Location` (`error` and `request` records, `last_error`) and backed off; for the archive it
    is not retried, like a 404.
  - A pass waits for a camera whose slot is near, inside its lock and right before the poll, and
    only until the clock reaches the slot. The wait is the **pass's own window, counted from the
    pass's start** (Lambda 45 s, `loop` 60 s, `once --wait-for-due S`): a camera whose slot falls
    later is left for the next pass (`not_due`). 0.5 s before a slot waited for, the host hold is
    read again and the poll reserved, so neither storage call delays the send.
  - On a schedule that fires exactly every 300 s (the Lambda's `rate(5 minutes)`, `loop --jitter
    0`), each poll lands its own elapsed time (plus 2 ms) later in its pass than the one before, so
    a camera is skipped about once per window/drift passes and polled near the next pass's start
    (a gap of ~555-620 s at that camera). The rate depends on request times and on the
    invocation jitter, neither measured on a deployed Lambda yet. Fake-clock simulations, 10 days,
    8 cameras, 45 s window: **1.1-1.4 skips per camera per day** with capture 0.2.0's answered
    request times and 0-2 s jitter (below); **1.95-2.98** with all 1,777 logged requests
    (failures included), 1 % spikes or a lognormal model; **2.3** with 0-10 s of jitter,
    **3.9** with 0-30 s, **36.6** with 0-60 s (183 of 2,880 passes then skip all 8 cameras); 6.5
    with requests of 0.45 s median (the review of 20 Nov 2026). 0 with the loop's default 30 s
    jitter. Measure EventBridge's jitter right after deployment.
- **Fetch on change only:**
  - ARD: when `imagedate` changes (a 304 on the feed means unchanged).
  - Rainier: when `ETag`/`Last-Modified` change.
  - The image GET is itself conditional on the last ETag.
- **At most `max_images_per_hour` (4) image GETs in any rolling hour, counted on answer times:**
  - A GET may be sent only while fewer than 4 of the camera's GETs have an answer time in the last
    3600 s (a bound in the future counts too). A camera's GETs run one at a time, so the 5th is
    sent no earlier than the 1st's answer + 3600 s.
  - Each GET is written to state before it is sent (`reserved_at`; a crash mid-download still
    counts), then given its send (`t`) and its answer (`answered_at`). A record with no answer
    counts from an upper bound (`state.get_answer_bound`, "Upgrade and crash bounds").
  - A pacing rule keeps each GET ≥ 0.8 × 3600/4 = 720 s after the last one's answer. Rainier has a new image at every 5-minute poll (it updates about once a minute, below), so it gets frames spread through the hour, not four in a row.
- **Back-off:** 5 min doubling to 1 h per camera, counted separately for the feed (`failures`), the image GET (`image_failures`) and the store (`store_failures`), so a feed that answers cannot reset a failing image. HTTP 429/503 also holds every request to www.nps.gov, live and archive (`host_backoff.py`), honouring `Retry-After`.
- **Never the same image twice, even when storing fails:** once a 200 JPEG has arrived its validators and `imagedate` are recorded as handled, also if a write then fails (a missing IAM grant, a full disk). The frame is lost and logged (`error`, stage `store`, with its digest) and polling backs off, so a broken store costs at most one GET per new image. Once a poll has started the camera state is saved however the poll ends.
- **Seeding:** sidecars already in `frames/<id>/` and `thumbs/<id>/` are folded into the state on the first run, and on every pass with local storage. That covers the Rainier stopgap's `http_last_modified`, `etag`, `sha256` and fetch times. The first production pass therefore neither re-downloads an unchanged image nor exceeds the hour's cap.
- **Concurrency:**
  - Each camera is handled under its storage lock (`flock` locally, a conditional put in S3).
  - A concurrent run skips a locked camera and never polls one that is not due.

**Why the rules hold at nps.gov.** nps.gov receives a request no later than it answers it, and the
next one no earlier than we send it. So with `A` the answer time of poll N and `S` the send of
poll N+1, nps.gov's gap is at least `S - A`, which the scheduler keeps ≥ 300.002 s whatever the
network does (connection set-up, a stalled leg, a retransmission). The request log shows it per
pair: `sent_at` of a poll minus `answered_at` of the one before is ≥ 300.002 s, exactly (both
are the strings the scheduler used). The same holds for the image cap: the 5th GET's `sent_at` is
≥ 3600 s after the 1st's `answered_at`. Measured on 0.2.0's own log (20 Nov from 02:39:39 UTC; the
send read as `t` − `elapsed_ms`, the answer as `t`; consecutive polls of a camera, both of the
`cli-loop` 0.2.0 inside the window), its 1 s send-to-send margin left 1 of 968 poll pairs to 15:17
UTC under 300 s on this provable basis (Acadia, 299.590 s; send to send, the minimum was 301.053 s;
to 12:57 UTC 1 of 751, a count that moves with the window's edges); 0.2.1's 2 ms send-to-send
margin would have left nearly all of them unprovable.

**A request that failed before any answer came back** (it may have been handed to the kernel,
and no response head arrived: a read timeout, a reset, Ctrl-C) proves nothing about when nps.gov
gets it. A queue in front of the application (an accept backlog, a busy worker, a front end
forwarding to the origin) can hand it on after the client gave up, and TCP keeps retransmitting a
head it could not deliver. The review of 20 Nov 2026 showed it over loopback with capture's own
code: the server read a timed-out poll 0.5-4 s after its recorded answer, so the next poll would
have reached it 296.0-299.5 s later (the 5-minute error back-off, `now + 300 s` truncated to the
second, falls at or before the poll slot and added nothing); with the bound below, 597.0-599.5 s
in the same runs (fixer's re-run of the review's scripts, 20 Nov). Such a failure now counts as
answered `fetch.FAILED_AFTER_SEND_BOUND_S` = 300 s after it (`last_answer_bound` in the state,
`answer_bound` on the GET record and, in the request record, `answer_bound: failed_after_send`
and `answer_bound_at` beside the reading in `answered_at`): the next poll waits ≥ 600.002 s from
the failure, and the image cap and pacing count the failed GET 300 s late. A failure that proves
nothing was written (no connection: DNS, TCP or TLS; no pooled connection; a send deadline that
had passed) and one after a response head (a body that timed out, was too large or too slow) are
answered when they failed, as before. In capture_log from 12:24 UTC on 19 Nov to 15:17 UTC on 20
Nov, 4 of 2,065 live requests failed: two connection resets while reading (55 and 594 ms), one
image read timeout (30.0 s) and one TLS handshake timeout (10.0 s, nothing sent). The first three
would now be bounded: at that rate about 2-3 a day across the 8 cameras, each costing its camera
one extra interval.

Exposures that remain, and their size:
- **A failed request nps.gov gets more than 300 s after we gave up** (a server-side queue that
  long, or a path that drops every retransmission for over 5 minutes and then recovers). Not
  bounded from the client; no such delay is in any log.
- **Clock rate:** the 2 ms margin covers ±6.7 ppm over 300 s. A disciplined clock is well inside
  that; a slew or step is covered below.
- **Clock offset between Lambda hosts:** every invocation is a new process (often in a new
  execution environment, maybe on another host) that reads answer stamps written on another
  clock. If its clock is δ ahead of the writer's, the gap at nps.gov is 300.002 s − δ: the rule
  assumes the hosts agree to within 2 ms. Not measurable offline. Each request record of a
  Lambda carries `lambda_log_stream` (the execution environment), so pairs whose answer and next
  send come from different environments can be checked after deployment; widen the Lambda's
  margin if they show offsets over 1 ms.

**Upgrade and crash bounds** (`state.CameraState.settle_unanswered`, run under the camera's lock
right after the state is loaded, before anything is sent; each change is logged as `answer_bound`).
An answer time is never taken earlier than the true one:
- **Send deadline:** a live request whose head would be written more than 30 s
  (`fetch.LIVE_SEND_DEADLINE_S`) after its reservation is abandoned unsent (`SendDeadlinePassed`,
  checked before the request starts and, through an httpcore trace hook, right before the head is
  written). It is checked on two clocks: the wall clock, against the reservation's stamp (the
  stamp the bound below is applied to), and a monotonic clock that keeps counting while the
  machine sleeps (`fetch.steady_clock`: `CLOCK_MONOTONIC_RAW` on macOS, `CLOCK_BOOTTIME` on
  Linux; Python's `time.monotonic()` on macOS is `mach_absolute_time()`, which stops during
  sleep, so a sleep between a reservation and its send used to let the request out an hour
  later). The polite client's own timeouts (`lookout_common.http.DEFAULT_TIMEOUT`: connect 10 s,
  read 30 s, write 30 s, pool 10 s) each bound one operation, not a request: a body that trickles in
  never times out, the TLS handshake gets 10 s per operation, and DNS is not covered at all. So the
  head of a request is handed to the kernel within 60 s of its reservation
  (`UNANSWERED_REQUEST_BOUND_S`: the deadline plus the 30 s write timeout); TCP can still
  retransmit it after that (the exposure above).
- **Total deadline:** a live body still arriving 30 s (a poll, `fetch.LIVE_POLL_DEADLINE_S`) or
  60 s (an image, `LIVE_IMAGE_DEADLINE_S`) after its request started is abandoned
  (`RequestDeadlinePassed`, checked when the response head arrives and after each chunk, so the
  last read can add one 30 s read timeout; a response head that itself trickles in is not cut short,
  which no web server does). In capture_log (12:24 UTC 19 Nov to 15:17 UTC 20 Nov) the 1,525 polls
  that did not fail took at most 3.8 s, the 536 image GETs at most 2.1 s. A camera visit (a slot wait of up to 60 s, a poll and an image GET)
  therefore ends within about 220 s plus its storage calls, inside the S3 camera lock's 300 s TTL:
  a trickling body can no longer let the lock expire under its holder (only possible for the CLI on
  S3; the Lambda dies at 120 s). The review's moto-S3 run of that case now ends with the image
  abandoned at its deadline (the first chunk past 60 s) and no lock taken over. The S3 state write is not yet conditional on the version it
  read (that is `lookout_common.storage`'s).
- **A poll reserved and never answered** (killed between the send and the save): counted as
  answered at the reservation + 60 s. A GET reserved and never answered: the later of its
  reservation and its send + 60 s.
- **Rolling back to code before 0.2.1** (the 0.2.0 snapshot) knows no reservation: it polls 301 s
  after `last_poll_at`. So while a poll is reserved, `last_poll_at` holds the reservation + 60 s
  as well (the send overwrites it with the real send stamp). After a crash between the send and
  the save, 0.2.0 then waits ≥ 300 s from the latest moment the crashed poll can have reached
  nps.gov; before, it polled at once (1.0 s and 10.0 s later at nps.gov in the review's runs of the
  real 0.2.0 code, 20 Nov 2026). A poll given up before its send puts the old `last_poll_at` back.
- **State from code that recorded no answer** (the running 0.2.0, or 0.2.1 before answer times):
  the last poll counts as answered at the later of its send stamp + 60 s and the moment this code
  first reads the state. That writer saved its state after its poll ended and before this code
  could take the lock, so the read time bounds the answer even for a 0.2.0 poll that crashed before
  its save (0.2.0 had no poll reservation). Its GET records (`t` is the reservation) count from
  `t` + 2 s + 60 s; the read time is not used there, since it would hold every camera's images back
  for up to an hour after the switch. Every 0.2.0 request in capture_log (19-20 Nov) finished within
  30.0 s of its send, its polls within 3.8 s.
- **What the switch from the running 0.2.0 costs:** each camera's first poll is due 300.002 s after
  the later of its last 0.2.0 send + 60 s and the restart. Restarted within seconds of a pass's end
  that is ~60-70 s more than a normal gap per camera (the review's upgrade simulation: switch gaps
  of 359.9-370.1 s at nps.gov, none under 300 s; eval's data report: a restart excess of 70.1 s),
  rising to ~300 s for a restart just before the next pass.
- **Cost of the reservation:** one more state write per poll: 8 a pass at 8 cameras (local: an
  fsync'd JSON write each), on Lambda 8 more S3 PUTs a pass (below).
- **A supervisor restart needs no delay for these rules:** they live in the state (and the S3 lock
  outlives the 120 s Lambda by its 300 s TTL). A delay of at least 1 s keeps the in-process 1 s
  spacing between any two live requests across a restart; a growing delay on repeated exits keeps a
  crash loop from re-reading the state every few seconds.

**Wall-clock steps.** The rules compare wall-clock stamps, which survive restarts.
- **Within a process** each answer is also read on the monotonic clock, and a rule uses the later
  of the two readings (`live._adjuster`): a forward step (or slew) of the wall clock cannot bring a
  poll or a GET early; a backward step only delays them by the step. The remaining error is the
  monotonic clock's own rate (`CLOCK_MONOTONIC_RAW` on macOS, not adjusted by NTP; crystal error
  of tens of ppm at most, ~15 ms over 300 s at 50 ppm). It keeps counting while the Mac sleeps:
  on `mach_absolute_time()`, which stops, every answer recorded before a sleep read that much
  later after the wake (the review measured the first image GET 613 s after a wake instead of
  1.5 s; on 20 Nov this Mac's `time.monotonic()` was 2,159,174 s behind `CLOCK_MONOTONIC_RAW`).
- **Across a restart** (a new loop, every Lambda invocation) only the wall clock is shared: a
  forward step between a camera's last answer and the new process's poll shortens that one gap by
  the step (`test_across_a_restart_a_forward_step_shortens_that_one_gap_by_the_step`). A backward
  step only delays. How Lambda hosts discipline their clocks is not documented in this repo (and
  their offsets are the exposure above); on this Mac, timed steps the clock only for large
  offsets, typically at wake from sleep.

**Measured: the schedules at the server** (20 Nov 2026; fake clock, no network: the real capture
code against `tests/receipts.py`'s fake nps.gov, which records when each request *arrives*. Each
request's time is drawn from capture 0.2.0's 1,024 answered requests in capture_log, 19-20 Nov:
polls 81 ms median warm, 120 ms on a cold connection (p95 169/508 ms, max 3.8 s), image GETs
209 ms; it is split at a random point into the leg before arrival and the leg after. 8 cameras;
"Lambda" is a fresh runner every 300 s with the 45 s window and 120 s timeout; "S3" adds 20 ms to
every storage call; "jitter" is 0-2 s on each invocation. Waiting is the billed sleep per pass.)

| Run | Poll pairs under 300 s at nps.gov | Shortest provable gap | Skips per camera per day | Pass (mean / max) | Waiting a pass | 5 GETs, shortest span at nps.gov |
|---|---|---|---|---|---|---|
| 0.2.1 before answer times (2 ms send to send), Lambda 24 h | 1,027 of 2,296 | 296.246 s | 0 | 25.3 / 29.8 s | 23.6 s | 3600.993 s |
| same, Lambda 10 days | 11,050 of 23,032 | 296.246 s | 0 | 39.1 / 50.2 s (grows: 26.9 → 45.3 s by quarter) | 37.2 s | 3601.059 s |
| **this code**, Lambda 24 h | 0 of 2,287 | 300.002 s | 1.12 | 33.8 / 47.8 s | 32.2 s | 3601.261 s |
| this code, Lambda 24 h, S3 | 0 | 300.002 s | 1.00 | 35.8 / 46.7 s | 31.5 s | 3601.112 s |
| this code, Lambda 24 h, jitter | 0 | 300.002 s | 1.00 | 35.4 / 48.3 s | 33.9 s | 3601.058 s |
| this code, Lambda 10 days | 0 of 22,918 | 300.002 s | 1.43 | 37.7 / 49.4 s (steady) | 36.0 s | 3601.015 s |
| this code, Lambda 10 days, S3 / jitter / both | 0 | 300.002 s | 1.49 / 1.46 / 1.50 | 37.9 / 37.3 / 37.4 s mean | 33.6 / 35.6 / 33.1 s | ≥ 3601.03 s |
| this code, Lambda 10 days, 30 s window | 0 | 300.002 s | 2.01 | 27.1 / 34.2 s | 25.5 s | 3601.015 s |
| this code, Lambda 10 days, 20 s window | 0 | 300.002 s | 3.33 | 19.3 / 26.4 s | 17.7 s | 3601.029 s |
| this code, Lambda 10 days, 10 s window | 0 | 300.002 s | 35.6 | 11.9 / 24.9 s | 10.5 s | 3601.4 s |
| 0.2.1 before, loop 72 h (30 s jitter) | 207 of 6,592 | 296.246 s | 0 | 11.7 / 21.5 s | | 3697.297 s |
| **this code**, loop 72 h (30 s jitter) | 0 of 6,592 | 300.002 s | 0 | 11.8 / 21.5 s | | 3697.297 s |
| 0.2.1 before, loop 72 h, `--jitter 0` | 3,269 of 6,912 | 296.246 s | 0 | 30.7 / 39.0 s | | 3600.86 s |
| this code, loop 72 h, `--jitter 0` | 0 | 300.002 s | 1.00 | 44.9 / 63.7 s | | 3601.064 s |

- **Skips are cheap in data:** each is one ~555 s gap; image GETs stayed at 94.2-95.2 a camera a day
  (of 96) at the 45 s window, 93.5-93.8 at 20 s, against 88.7-90.0 for the code before answer times
  in the same runs (its GETs locked onto the hourly cap's edge). With 1 % spikes of 2-8 s: 2.0 skips
  a camera a day on the Lambda, 0 in the loop, and still 0 pairs under 300 s at nps.gov (the code
  before answer times: 1,081 of 2,296 and 185 of 6,576, down to 292.6 s).
- **The window is the pass's, from its start:** counted from each camera's visit instead (0.2.1),
  a pass waited for each later camera anew; in the same 10-day run passes then reached 78.1 s
  (p99 74.0 s) against 49.4 s (p99 46.4 s), and 89 s with 2 % spikes, near Lambda's 95 s reserve.
  49.4 s is the maximum only with the answered requests above: with all 1,777 logged requests,
  failures included (a 10.0 s TLS timeout, a 30.0 s image read timeout), the review's 10-day run
  reached **83.5 s** (5 of 2,880 passes over 60 s, none over 95 s; p99 47.7 s), and its other runs
  69.6-76.6 s (p99 46.5-51.9 s). 0.2.0 in the same runs: 96-135.9 s, with 146 `time_left` skips.
- **The archive queue's time between passes** (D-029) is unchanged with the loop's default jitter:
  303.1 s a pass on average (shortest 278.6 s), before and after. With `--jitter 0`: 255.1 s against
  269.3 s.
- **Cost** (infra's `cost/sim_capture_s3.py` on an export of this tree, then `cost/estimate.py`):
  the poll reservation is one more S3 PUT per poll: 11,048 PUTs and 10,176 GETs a day, against
  8,744 and 10,176 for 0.2.1 before answer times (+2,304 PUTs, the 288 passes × 8 cameras), and
  against **8,944 and 7,902 for the committed 0.2.0 that D-060 priced (+2,104 PUTs, +2,274 GETs a
  day)**; state and lock PUTs 7,624 a day against D-060's 5,360 ($1.14 against $0.80 a month).
  Capture's waiting at the 45 s window is about 36 s a pass in steady state (the zero-latency
  infra simulation shows 14.6 s, as for 0.2.1, because no request takes time there; with real
  request times 0.2.1 itself creeps to 37.2 s over 10 days; with all logged requests, failures
  included, the review measured 75.6 s for 0.2.1 before answer times and 62.3-79.3 s for 0.2.0,
  against 29.8-36.7 s for this code). D-060's total with the free tiers becomes $2.43-4.40 in month 1
  (D-060: $2.07-4.03; the review's range for 29.8-36.7 s of waiting: $2.43-4.08 to $2.43-4.44);
  at a 20 s window, $2.43-3.35. **The bottom of the range now passes the $3 hard cap in month 4,
  not month 6, and the top of the range reaches the hard stop on day 25.7 of month 1 at 36 s of
  waiting (28.8 at a 20 s window; D-060: day 26.7)** (`estimate.py` on this tree's simulation,
  re-run 20 Nov after the review's fixes, which add no storage call: the same 11,048 PUTs and
  10,176 GETs).

**Archive** (`archive.py`):
- Every request goes through `archive_limiter()`, a file-mode `MinIntervalLimiter("nps-archive", 5 s).hold()`: one at a time across processes, ≥ 5 s end to start. It is file-based even with `LOOKOUT_STORAGE=s3` (common's `auto` mode would fall back to an in-process limiter there).
- Errors are deferred for every process. A 200 whose body is not a decodable JPEG (an HTML error page, a truncated file) is an error too: not stored, not marked done.
- A failing asset (transport error, HTTP 429/5xx, bad body) is tried at most 3 times per run and 6 times in all; failures are kept in `state/archive/<id>.json` under `failed`. A 404 is not retried.
- An optional persisted request budget applies.
- An asset already stored, or listed in `state/archive/<id>.json`, is never requested again.
- `--max-gb` caps the bytes downloaded per viewpoint across runs (bad bodies count).
- A day's search is cached under `cache/nps-archive/` only once the day is at least two days old (UTC), and a cache entry written earlier than that is ignored and refetched.

## Storage (PRD §8.7, CLAUDE.md layout, via `lookout_common.storage`)

**What is kept** (`retention.py`, the lead's decision of 19 Nov 2026):
- **Every download, every camera:** a 640 px JPEG thumbnail (Lanczos, q85, original EXIF) with its sidecar
  at `thumbs/<id>/<UTC date>/<stamp>_<sha256[:12]>.jpg`. Its sidecar is the capture's sidecar with the
  file fields (`stored*`, `jpeg_quality`) describing the thumbnail, `variant: "thumb"`, and a
  `full_frame` block (the full-size copy's policy and file fields). At an "every" camera
  `full_frame.key` is the frame in `frames/`. At an hourly camera the full frame is kept only if it is
  its hour's nearest, so there is no `key`: `key_if_promoted` is where it would be (a promoted frame's
  sidecar carries `hourly_selection`) and `staged_key` where it waits. An undecodable image gets none.
- **`rainier.paradise-mountain` and `point-reyes.beach`** (design P, D-024): every frame at ≤ 1920 px in
  `frames/`, as before (`frame_policy: "every"`).
- **The other six** (`frame_policy: "hourly-nearest"`): each download is staged at full size under
  `cache/staging/frames/<id>/...` and listed in the camera's state. Hour `H` takes image times in
  `[H − 30 min, H + 30 min)` and is decided at `H + 60 min` (no candidate can still be on its way: an
  ARD frame is on the feed ~7 min after its image time and polled within 5.5 min). The decodable
  candidate nearest `H` is promoted to `frames/` (JPEG, then its sidecar with an `hourly_selection`
  block; both create-only); ties go to the earlier frame; the hour's other staged copies are deleted.
  If `frames/` already has a frame in that window (older code kept every frame, or a crash cut a
  promotion short, which is then completed), it stays and the staged copies are dropped.
- **Never deleted:** anything under `frames/`, `archive/` or `thumbs/`. Only `cache/staging/` copies
  are ever deleted (`delete_staged` refuses any other key). Existing frames stay as they are.
- **Crash safety:** at an hourly camera writes are ordered (thumbnail and its sidecar, which can
  rebuild the frame's, before the staged frame), staging persists across restarts, and the staging
  folder is listed every pass locally (every 6 h with S3, and on a first run), so a copy a crash left
  out of the state is still decided on the next pass. At an "every" camera the frame comes first; a
  thumbnail whose write failed, or that a crash cut short (found when the frame's sidecar is folded
  in), is listed in `thumbs_pending` and made from the stored frame, two a pass
  (`thumb_made_from: "stored_frame"`; `thumb_repaired`, or `thumb_given_up` after 3 tries). A crash
  fuzz (`tests/test_crash_fuzz.py`, 10 seeds in the suite, 120 more run once) checks that nothing under
  `frames/` or `thumbs/` is lost or rewritten, no hour keeps two frames, and each decided hour keeps
  the nearest frame staged by then.
- A `stored` record's `key` is the download's durable file: the frame for "every" cameras, the
  thumbnail for hourly ones (`frame_key`, `thumb_key` and `staged_key` are also given).

**Measured** (`lookout-capture storage --day 2026-11-19`, 22:38 UTC, on the 105 frames on disk from
06:30–22:23 UTC 19 Nov; thumbnails made in memory with the code above; Sun-up/down means weighted by
that day's daylight share; 96 downloads a camera a day at the 4-per-hour cap):

| Camera | Policy | Frame (mean, n) | Thumbnail (mean) | Old MB/day | New MB/day |
|---|---|---|---|---|---|
| `rainier.paradise-mountain` | every | 825 KB (n=32; Sun up 1,687 KB, n=6) | 34 KB (76 KB Sun up) | 96.4 | 100.8 |
| `point-reyes.beach` | every | 467 KB (n=11) | 39 KB | 50.8 | 55.4 |
| `grand-canyon.yavapai` | hourly | 483 KB (n=11) | 46 KB | 55.2 | 19.3 |
| `yosemite.turtleback` | hourly | 445 KB (n=9) | 37 KB | 48.9 | 16.8 |
| `acadia.mcfarland` | hourly | 527 KB (n=11) | 41 KB | 55.5 | 18.7 |
| `grand-teton.science-center` | hourly | 452 KB (n=10) | 40 KB | 46.8 | 16.7 |
| `smokies.look-rock` | hourly | 407 KB (n=11) | 36 KB | 38.6 | 13.3 |
| `big-bend.panther-junction` | hourly | 618 KB (n=10) | 59 KB | 59.7 | 21.1 |
| **All 8** | | | | **451.9** | **262.0** (0.58×) |

Sidecars: 2.4–2.6 KB now; under the new code 2.8–3.0 KB (frame), 2.8–3.0 KB at the two "every"
cameras and 3.2 KB at the hourly ones (thumbnail) and 3.5–3.7 KB (promoted frame), re-measured after
the `full_frame` change (totals unchanged). Daytime frames are smaller than night (IR) frames at six of
the seven ARD cameras (at Big Bend by only 1 %: 614 against 622 KB); at Smokies they are larger (418
against 387 KB). Over 90 days: 40.7 GB old, 23.6 GB new; PRD §8.7 estimated ~135 MB a day, which
the six hourly cameras meet (13–21 MB each, PRD ~17) while the two design-P cameras keep every frame.

**Files and sidecars:**
- `frames/<id>/<UTC date>/<YYYYMMDDTHHMMSSZ>_<sha256[:12]>.jpg` + `.json` sidecar:
  - The original bytes are kept if the image is at most 1920 px wide.
  - Otherwise a Lanczos 1920 px copy is stored at JPEG q90, with the original EXIF bytes.
  - The sidecar holds:
    - HTTP validators and the JSON `imagedate` (raw and UTC);
    - the EXIF fields;
    - the chosen image time, its source, and the minutes between the candidate times;
    - original and stored sizes and SHA-256 digests;
    - dHash64, `dhash_bits_to_previous`, mean luma, chroma spread and `sharpness_lapvar`;
    - `sun`: geometric and apparent Sun elevation at the image time (NOAA equations), `daylight` = apparent ≥ 3° (PRD §13.3);
    - flags: `decode_failed`, `dimensions_changed`, `duplicate_sha`, `stale_feed`, `tiny_file`, `monochrome`, `sun_below_3deg`, time flags;
    - `frame_policy`, and a `thumb` block (key and file fields of the thumbnail); a promoted frame also has `hourly_selection`.
- Live sidecars are `sidecar_schema` 2 (additive over 1: `frame_policy`, `thumb`, and in thumbnails `variant`/`full_frame`, in promoted frames `hourly_selection`), written by `capture_version` `lookout-capture/0.2.0`; frames of the frozen 0.1.0 loop say 0.1.0 and schema 1.
- `archive/<id>/<UTC date>/<stamp>_<assetid[:8]>.jpg` (hires) or `..._thumbmedium.jpg` + `.json`.
- `capture_log/<UTC date>.jsonl`:
  - capture events: `request` (one per nps.gov request: stage, method, status, bytes, ms, `sent_at`, `answered_at`; a failure that may still reach nps.gov also `answer_bound: failed_after_send` and `answer_bound_at`; a 3xx its `location`; on Lambda `lambda_log_stream`), `answer_bound` (an answer this code had to bound: a reservation a crash left unanswered, or state from code that recorded no answer; a GET's own stamps as `get_t`/`get_reserved_at`, since every record's `t` is the moment it was written), `stored`, `unchanged`, `duplicate`, `rate_capped`, `error` (stage `head`, `json`, `image`, `store` or `internal`), `backoff`, `promoted`, `staging_dropped` (with a reason), `staging_error`, `thumb_repaired`, `thumb_given_up`, `pass`, `idle_error`, `loop_stopped`. Every poll outcome carries the feed's validator (`json_last_modified`, or `head_last_modified`/`http_last_modified`), for `cadence`;
  - archive events: `archive_request`, `stored`, `error`, `held_out_override`, `queue_done`, `queue_stopped`, `queue_refused`, `queue_failed`, `queue_total_cap` (once per process). Each carries `runner` as live records do: `cli-loop` (the queue inside the loop), `cli-archive`, `cli-archive-check`.
  - `state/capture/hosts/www.nps.gov.json`: the www.nps.gov hold (`not_before`, `reason`, `source`: `live` or `archive`).
- `LOOKOUT_STORAGE=s3` runs the same code against S3. The Lambda entry point is `lookout_capture.lambda_handler.handler`.
  - **One log write per pass:** with S3 every record of a pass, the `pass` record included, is written by one `append_jsonl_many` when the pass ends (each S3 append is a GET and a PUT of the day's whole log). Locally each record is appended at once. Records a failed write could not store are printed to stderr as JSON lines (`capture_log_unwritten`; CloudWatch keeps a Lambda's) and sent first with the runner's next pass (up to 2,000); with less than 2 × 25 s left, the records so far are written before each further camera.
  - **IAM:** the capture role needs read/write on `thumbs/*` and `cache/staging/*`, and `s3:DeleteObject` on `cache/staging/*`. They are granted in `infra/terraform/main.tf:59-60` (committed in e59247a, D-059), with a terraform test of the grants. Without them every download at the six hourly cameras would fail to store (still never fetched twice, but its frame lost). Deleting staged copies in S3 goes through the client inside `S3Storage` until `lookout_common` has a delete for `cache/` keys (TODO(M1)).

## Image time

Candidates, in order of preference:
1. EXIF `DateTimeOriginal`, using its offset if present. Otherwise it is local wall time in the manifest zone, with DST handled; in a fold, the occurrence nearest the NPS `imagedate` wins.
2. The JSON `imagedate`, whose zone abbreviation fixes the offset.
3. HTTP `Last-Modified`.
4. The fetch time, which is flagged.

EXIF is overruled, and flagged `exif_time_overruled`, only for the shape of a camera clock on the wrong DST rule: EXIF is within 2 min of exactly one hour from `imagedate`, and `Last-Modified` follows `imagedate` by −2 to +20 min (6.5–6.9 min measured at the ARD cameras). Any other gap over 30 min keeps EXIF and is flagged `time_conflict`. A `Last-Modified` more than 20 min after the chosen time is flagged `last_modified_long_after_image_time`.

## Quality gates (PRD §8.6) at capture time

| Gate | What capture records | Status |
|---|---|---|
| Fresh | `duplicate_sha`, `stale_feed`, `stale_image_time`, `same_last_modified_new_bytes`; unchanged `imagedate`/validators are never fetched | done |
| Same view | `dhash_bits_to_previous` (no flag; weather alone moves midday dHash by up to 43 bits between days, typically 15–35) | TODO(M2): plate registration |
| Clear lens | `sharpness_lapvar`, Laplacian variance at ≤ 640 px. No flag: measured 19 Nov, two foggy midday archive frames read 2.4–2.7, seven other midday archive frames 31–2,074, and night frames 1,031–1,638 (sensor noise) | TODO(M2): per-viewpoint floor on development frames |
| Colour mode | `monochrome` when chroma spread < 1.0 (27 night frames 0.00–0.26; 85 pre-guard midday frames 2.46–91.4) | done; TODO(M2) re-check on fog |
| Daylight | `sun` block and `sun_below_3deg` (apparent Sun < 3°) | done; scoring recomputes with `oculus-core` |

## Update cadence (`cadence.py`)

`lookout-capture cadence <id>` reads `capture_log` (the capture worker's poll outcomes and `request`
records, and the D-021 stopgap's records) and the sidecars, so it needs no request. Each poll gives at
most one value: the image `Last-Modified` from the HEAD (park camera), or the JSON feed's `Last-Modified`
and `imagedate` (ARD). Two measures per series:
- **Intervals:** the change between two consecutive polls at most 1.8 poll intervals apart; longer
  gaps (laptop asleep) and polls whose value was not logged break the pair, and an HTTP 304 repeats
  only the poll just before it, when that one is known.
- **Age at poll:** the server's answer time minus the value. Each age is a lower bound on the gap
  between updates around that poll.

`poll_spacing_s` is send to send, from each poll's `request` record (`sent_at`, or `t` − `elapsed_ms` for
0.2.0's records); outcome records come after the answer and any image GET, so spacing read from them
can show under 300 s for polls nps.gov saw 300 s apart (the review found 298.4 s that way).

When (nearly) every pair changed (`poll_limited`: at most 10 % unchanged) the camera updates at least
as often as it is polled and the intervals only measure the poll spacing; the age at poll is then the
measurement, and its maximum says how old the newest update was at worst, at the polls sampled.
Otherwise the intervals are the camera's. The output also gives `minute_mod_10_of_values` (writes at
fixed minute marks) and whether the values lie on a one-minute grid (≥ 50 % of intervals within 3 s
of whole minutes, against 12 % by chance). Server `Last-Modified` is compared with this machine's
clock, so a negative age is clock offset.

Measured 19 Nov 2026 to 22:37:29 UTC (the window of the first measurement), `--hours 24`:
- **Rainier, HEAD `Last-Modified`** (65 polls: 41 stopgap, 24 loop; 48 with a value): poll-limited.
  All 37 decided consecutive pairs changed, so their intervals (median 300.0 s, p10 239.6 s, p90
  359.4 s) are our 5-minute poll spacing, not the camera's. **Age at poll: median 20.6 s, p90 69.0 s,
  max 125.9 s** (n=48; min −0.1 s); 40 of 48 at most 60 s (30 s bins: 1 below 0, 29, 10, 6, 1, 1).
  The values lie on a one-minute grid (29/37 intervals within 3 s of whole minutes; upload second
  :09–:28). So **Rainier updates about once a minute**: at every poll its newest image was at most
  126 s old, and at 40 of 48 at most a minute. To 23:57 UTC: n=63, median 20.7 s, p90 70.3 s, max
  125.9 s.
- **The seven ARD cameras, `imagedate`:** every 900 s (median, n=6–7 each; one 1800 s gap at
  Yosemite); the newest image is 4.5–25 min old when polled.
- **The seven ARD cameras, JSON `Last-Modified`:** the file is rewritten twice every 10 minutes, at 1–2
  and at 5 minutes past each 10-minute mark (every one of 209 distinct values to 23:57 has a minute of
  1, 2 or 5 mod 10). Intervals are bimodal, ~200–230 s and ~370–400 s, so a median is not a cadence;
  the age at poll is at most 381–394 s to 22:37. The rewrite costs one 2.3–4.7 KB JSON GET per 5-min
  poll (a 200 each time) and never an image GET.

## Known limits

- **`frames/` lags up to an hour at the six hourly cameras:** hour `H` is decided at `H + 60 min`, so
  the newest full-size frame is in `cache/staging/` until then (its thumbnail is in `thumbs/` at once).
  A live view (PRD §11.1 `GET /v1/live`) should serve the newest thumbnail or staged frame.
- **Staged frames live under `cache/`,** which CLAUDE.md calls deletable: clearing `cache/staging/`
  loses at most the undecided hours' full-size frames (about 1.5 h, ~4 MB a camera); their thumbnails
  stay. In S3 the `cache/` lifecycle rule expires objects after 7 days, far longer than a decision takes.
- **Archive work between passes is checked against a stopgap only before each pass:** a foreign
  fetcher appearing mid-window is seen at the next pass, at most ~5 min later.
- **A download that cannot be stored is lost, not retried:** fetching it again would break "download
  only on change". At an hourly camera a failed staged write can leave its thumbnail without a full
  frame; a failed thumbnail write there loses the download (both are logged as `error`, stage `store`).
- **A thumbnail remade from a stored frame** is encoded from the stored 1920 px JPEG, not from the
  pixels before it, so it can differ from the one live capture would have made by a few bytes; the
  frame's own sidecar still describes the thumbnail that could not be written.
- **The hour decision does not wait for a stalled feed:** an image shown again long after its hour
  was decided is staged, then dropped as `hour_already_kept` (its thumbnail stays).
- **The switch from 0.1.0 (every frame) to 0.2.0 kept one far frame per camera for one hour:** an
  hour that already holds a frame in `frames/` keeps it (`frames/` is append-only), and its nearer
  staged candidates are dropped as `hour_already_kept`. On 20 Nov 2026 the loop switched at
  02:39:39 UTC; for hour 03:00 UTC, Acadia, Grand Canyon, Grand Teton and Yosemite kept 0.1.0's
  `20261120T023000Z` frame (1800 s from the hour) as the hour's only full-size frame, and their
  02:45, 03:00 and 03:15 frames survive only as 640 px thumbnails (decided 04:04 UTC). Big Bend and
  the Smokies promoted their 03:00 frame. A frame there without `hourly_selection` in its sidecar
  comes from the every-frame code; eval should not read it as the frame nearest its hour.
- **Polls drift on an exact 300 s schedule, and the Lambda bills the wait:** every poll waits for
  the last answer + 300.002 s, so on `rate(5 minutes)` each lands its elapsed time later than the
  last until its pass skips it (one gap of ~555-620 s at that camera). Measured on a fake clock
  (tables above): 1.1-1.4 skips per camera per day and 36 s of billed waiting a pass at the
  Lambda's 45 s window with 0.2.0's answered request times; 2.0-3.0 with other latency models;
  3.9 with 0-30 s of invocation jitter and 36.6 with 0-60 s. A 20 s window halves the waiting
  (18 s) for 3.3 skips per camera per day. Skips cluster: in the review's all-requests run 109
  passes skipped a camera, 25 of them 2-6 cameras at once. Whether design P counts outages per
  pass or per camera decides whether this matters before the sealed window.

- Archive `Search` paging is not working: page 2 returned HTTP 500 both at `/Execute/<SearchID>` and with `Page=2` in the body (2026-11-19). A single day holds at most 96 frames, under the 300 page size, so backfill is unaffected. An incomplete search is never cached.
- The archive "hires" is about 999 px wide, not the 4K live frame (see `docs/research/nps-archive-check.md`).
