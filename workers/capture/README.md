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
| `lookout-capture status [--json] [--archive-queue PATH]` | none | Persisted state per camera (the frame policy the newest frame's writer used, marked `*` where this code would use another; frames staged; last hourly decision; store failures; thumbnails pending), today's event counts, and the archive queue's progress. |
| `lookout-capture archive queue add <id> <YYYY-MM-DD>... [--through YYYY-MM-DD] [--hires] [--thumbs] [--pick all\|hourly\|midday] [--daytime-only] [--min-sun-deg X] [--max-gb 1] [--queue PATH]` | none | Queue days to backfill (`--through`: every day of a range, at most 400). A day on or after 1 Jun 2026 is refused (D-019). |
| `lookout-capture archive queue list [--json]` / `remove <id>... [--finished] [--all]` / `resume <id>... [--all] [--max-gb G]` | none | Show or edit the queue (default `state/archive/queue.json`). `--finished` takes done, refused, failed and stopped entries; `resume` sets stopped (size cap) or failed entries back to work, `--max-gb` raising their cap. Safe while the loop runs. |
| `lookout-capture cadence <id> [--hours 24] [--max-gap S] [--json]` | none | The camera's update cadence from `capture_log` and sidecars (below). |
| `lookout-capture storage [--day YYYY-MM-DD] [--only <id>] [--json]` | none | Bytes per frame and thumbnail per camera from disk, and bytes per day under PRD §8.7 against the old policy. Writes nothing. |
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

**Held-out guard** (D-019, `heldout.py`). Archived frames, full size or thumbnail, dated on or
after **1 Jun 2026** (local date, the latest of `ImageCreateDateTime`, `LocalTimeString` and
the civil date of the UTC time) are never requested or read. `archive day|sample` refuses such
dates outright; `--dry-run` reads metadata only and lists the refusals. Only scoring may pass
`--held-out-override "<reason>"`; the reason is logged (`held_out_override`) with every frame
it releases and written into the sidecar.

## Rules the code enforces

**Live** (`live.py`, state in `state/capture/<id>.json` under the lock `state/locks/capture/<id>.lock`):
- **Polling:**
  - ARD air cams: a conditional GET of the JSON feed.
  - Rainier park camera: a HEAD on the image.
  - At most once per `poll_interval_s`, never under 300 s.
- **Fetch on change only:**
  - ARD: when `imagedate` changes (a 304 on the feed means unchanged).
  - Rainier: when `ETag`/`Last-Modified` change.
  - The image GET is itself conditional on the last ETag.
- **At most `max_images_per_hour` (4) image GETs in any rolling hour:**
  - Each GET is written to state before it is sent.
  - A pacing rule keeps GETs ≥ 0.8 × 3600/4 = 720 s apart. Rainier has a new image at every 5-minute poll (it updates about once a minute, below), so it gets frames spread through the hour, not four in a row.
- **Back-off:** 5 min doubling to 1 h per camera, counted separately for the feed (`failures`), the image GET (`image_failures`) and the store (`store_failures`), so a feed that answers cannot reset a failing image. HTTP 429/503 also holds every request to www.nps.gov, live and archive (`host_backoff.py`), honouring `Retry-After`.
- **Never the same image twice, even when storing fails:** once a 200 JPEG has arrived its validators and `imagedate` are recorded as handled, also if a write then fails (a missing IAM grant, a full disk). The frame is lost and logged (`error`, stage `store`, with its digest) and polling backs off, so a broken store costs at most one GET per new image. Once a poll has started the camera state is saved however the poll ends.
- **Seeding:** sidecars already in `frames/<id>/` and `thumbs/<id>/` are folded into the state on the first run, and on every pass with local storage. That covers the Rainier stopgap's `http_last_modified`, `etag`, `sha256` and fetch times. The first production pass therefore neither re-downloads an unchanged image nor exceeds the hour's cap.
- **Concurrency:**
  - Each camera is handled under its storage lock (`flock` locally, a conditional put in S3).
  - A concurrent run skips a locked camera and never polls one that is not due.

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
  - capture events: `request` (one per nps.gov request: stage, method, status, bytes, ms), `stored`, `unchanged`, `duplicate`, `rate_capped`, `error` (stage `head`, `json`, `image`, `store` or `internal`), `backoff`, `promoted`, `staging_dropped` (with a reason), `staging_error`, `thumb_repaired`, `thumb_given_up`, `pass`, `idle_error`, `loop_stopped`. Every poll outcome carries the feed's validator (`json_last_modified`, or `head_last_modified`/`http_last_modified`), for `cadence`;
  - archive events: `archive_request`, `stored`, `error`, `held_out_override`, `queue_done`, `queue_stopped`, `queue_refused`, `queue_failed`, `queue_total_cap` (once per process).
  - `state/capture/hosts/www.nps.gov.json`: the www.nps.gov hold (`not_before`, `reason`, `source`: `live` or `archive`).
- `LOOKOUT_STORAGE=s3` runs the same code against S3. The Lambda entry point is `lookout_capture.lambda_handler.handler`.
  - **One log write per pass:** with S3 every record of a pass, the `pass` record included, is written by one `append_jsonl_many` when the pass ends (each S3 append is a GET and a PUT of the day's whole log). Locally each record is appended at once. Records a failed write could not store are printed to stderr as JSON lines (`capture_log_unwritten`; CloudWatch keeps a Lambda's) and sent first with the runner's next pass (up to 2,000); with less than 2 × 25 s left, the records so far are written before each further camera.
  - **IAM:** the capture role needs read/write on `thumbs/*` and `cache/staging/*`, and `s3:DeleteObject` on `cache/staging/*` (infra/terraform/main.tf lists neither yet). Without them every download at the six hourly cameras fails to store: it is still never fetched twice, but its frame is lost, so the grants must land before a Lambda image with this code. Deleting staged copies in S3 goes through the client inside `S3Storage` until `lookout_common` has a delete for `cache/` keys (TODO(M1)).

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

- Archive `Search` paging is not working: page 2 returned HTTP 500 both at `/Execute/<SearchID>` and with `Page=2` in the body (2026-11-19). A single day holds at most 96 frames, under the 300 page size, so backfill is unaffected. An incomplete search is never cached.
- The archive "hires" is about 999 px wide, not the 4K live frame (see `docs/research/nps-archive-check.md`).
