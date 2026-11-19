# lookout-capture

Polite capture of the 8 NPS cameras in `viewpoints/manifests/` (PRD §8.6, §11.2, §15.3), the NPS
AirWebCams archive backfill, and the M0.5 archive check (PRD §13.2, §13.9). Python 3.13, uv.

```sh
cd workers/capture && uv sync && uv run pytest -q && uv run ruff check .
```

## Commands

| Command | Network | What it does |
|---|---|---|
| `lookout-capture once [--only <id>]` | NPS live | One pass over the cameras that are due. Prints a JSON summary. |
| `lookout-capture loop [--interval 300] [--jitter 30]` | NPS live | A pass every interval plus 0–jitter s. The first Ctrl-C finishes the current camera and exits; a second interrupts at once. |
| `lookout-capture status [--json]` | none | Persisted state per camera and today's event counts. |
| `lookout-capture archive days <id>` | NPS archive | `GetAvailableDays`: first and last day, days per year. |
| `lookout-capture archive day <id> <YYYY-MM-DD> [--daytime-only] [--hires] [--dry-run] [--max-gb 1] [--held-out-override REASON]` | NPS archive | One day's frames (thumbmedium unless `--hires`). |
| `lookout-capture archive sample <id> <YYYY-MM-DD>... [--pick midday\|hourly\|all] [--held-out-override REASON]` | NPS archive | Picked frames of several days. |
| `lookout-capture archive-check [--viewpoint <id>]... [--rainier-page] [--budget 220]` | NPS archive | The M0.5 check; writes `archive/archive_check_<date>.json`. |

**One NPS fetcher per machine** (CLAUDE.md, D-021). Every command except `status` runs inside a
host-wide lock, `$LOOKOUT_DATA_DIR/state/locks/nps-fetcher.lock` (`flock`, always on local disk,
also with `LOOKOUT_STORAGE=s3`). It exits with **status 3**, before sending anything, when:
- another `lookout-capture` fetcher holds the lock (the message names its pid and command);
- a Rainier stopgap process (`stopgap_rainier.py`) is running;
- stopgap-shaped records (`"vp"`, no `"worker"`) are in the local `capture_log` from the last
  11 minutes. After the stopgap is stopped, wait out that window (the message gives the time).

`loop` repeats the stopgap check before every pass and stops (status 3) if one appears.

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
  - A pacing rule keeps GETs ≥ 0.8 × 3600/4 = 720 s apart. Rainier changes every 5 minutes, so it gets frames spread through the hour, not four in a row.
- **Back-off:** 5 min doubling to 1 h per camera, counted separately for the feed (`failures`) and the image GET (`image_failures`), so a feed that answers cannot reset a failing image. HTTP 429/503 also pauses every camera on www.nps.gov, honouring `Retry-After`.
- **Seeding:** sidecars already in `frames/<id>/` are folded into the state on the first run, and on every pass with local storage. That covers the Rainier stopgap's `http_last_modified`, `etag`, `sha256` and fetch times. The first production pass therefore neither re-downloads an unchanged image nor exceeds the hour's cap.
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

## Storage (CLAUDE.md layout, via `lookout_common.storage`)

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
    - flags: `decode_failed`, `dimensions_changed`, `duplicate_sha`, `stale_feed`, `tiny_file`, `monochrome`, `sun_below_3deg`, time flags.
  - Every frame is kept at ≤ 1920 px. PRD §8.7 asks for 1920 px only for the frame nearest each hour, plus 640 px thumbnails; this keeps more, and the thumbnails can be derived later (TODO(M1)). Measured on 19 Nov: 595–667 KB per stored frame, about 0.5 GB a day for 8 cameras at the 4-per-hour cap, against the PRD's 135 MB. The deviation needs a DECISIONS entry.
- `archive/<id>/<UTC date>/<stamp>_<assetid[:8]>.jpg` (hires) or `..._thumbmedium.jpg` + `.json`.
- `capture_log/<UTC date>.jsonl`:
  - capture events: `request` (one per nps.gov request: stage, method, status, bytes, ms), `stored`, `unchanged`, `duplicate`, `rate_capped`, `error`, `backoff`, `pass`, `loop_stopped`;
  - archive events: `archive_request`, `stored`, `error`, `held_out_override`.
- `LOOKOUT_STORAGE=s3` runs the same code against S3. The Lambda entry point is `lookout_capture.lambda_handler.handler`.

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

## Known limits

- Archive `Search` paging is not working: page 2 returned HTTP 500 both at `/Execute/<SearchID>` and with `Page=2` in the body (2026-11-19). A single day holds at most 96 frames, under the 300 page size, so backfill is unaffected. An incomplete search is never cached.
- The archive "hires" is about 999 px wide, not the 4K live frame (see `docs/research/nps-archive-check.md`).
