"""lookout-capture: polite NPS webcam capture for Lookout (PRD §8.6, §11.2, §15.3).

Modules:

- :mod:`.live`: one capture pass over the manifest cameras (fetch on change, at most
  4 images per camera per rolling hour, persisted state, back-off).
- :mod:`.frames`: decode, time-stamp, downscale and fingerprint one downloaded image.
- :mod:`.state`: the persisted per-camera state and the rate-cap rules.
- :mod:`.archive`: the NPS AirWebCams archive API and the resumable backfill.
- :mod:`.archive_check`: the M0.5 archive depth, cadence and epoch check (PRD §13.2, §13.9).
- :mod:`.sun`: a small solar-position routine used only to check archive time stamps.
- :mod:`.lambda_handler`: the AWS Lambda entry point; :mod:`.cli`: the console script.
"""

__version__ = "0.1.0"

#: Written into every sidecar and log record, so frames trace back to the code that took them.
CAPTURE_VERSION = f"lookout-capture/{__version__}"

__all__ = ["CAPTURE_VERSION", "__version__"]
