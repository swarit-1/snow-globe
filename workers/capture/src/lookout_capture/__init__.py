"""lookout-capture: polite NPS webcam capture for Lookout (PRD §8.6, §11.2, §15.3).

Modules:

- :mod:`.live`: one capture pass over the manifest cameras (fetch on change, at most
  4 images per camera per rolling hour, persisted state, back-off).
- :mod:`.frames`: decode, time-stamp, downscale, thumbnail and fingerprint one downloaded image.
- :mod:`.retention`: PRD §8.7 storage: thumbnails, every frame at the two design-P cameras,
  the frame nearest each UTC hour at the others (staging and promotion).
- :mod:`.state`: the persisted per-camera state and the rate-cap rules.
- :mod:`.archive`: the NPS AirWebCams archive API and the resumable backfill.
- :mod:`.archive_queue`: the backfill queue, worked between live passes (D-023).
- :mod:`.archive_check`: the M0.5 archive depth, cadence and epoch check (PRD §13.2, §13.9).
- :mod:`.cadence` and :mod:`.storage_report`: no-network measurements of a camera's update
  cadence and of bytes stored per day.
- :mod:`.sun`: a small solar-position routine (NOAA equations) for the Daylight gate.
- :mod:`.host_backoff`: one HTTP 429/503 back-off for www.nps.gov, live and archive alike.
- :mod:`.lambda_handler`: the AWS Lambda entry point; :mod:`.cli`: the console script.
"""

__version__ = "0.2.0"

#: Written into every sidecar and log record, so frames trace back to the code that took them.
#: 0.2.0 (19 Nov 2026): PRD §8.7 storage (thumbs/, staging, hourly promotion), the archive
#: queue in the loop, one www.nps.gov back-off, live sidecar schema 2.
CAPTURE_VERSION = f"lookout-capture/{__version__}"

__all__ = ["CAPTURE_VERSION", "__version__"]
