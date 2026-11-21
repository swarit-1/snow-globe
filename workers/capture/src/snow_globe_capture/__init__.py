"""snow-globe-capture: polite NPS webcam capture for Snow Globe (PRD §8.6, §11.2, §15.3).

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

__version__ = "0.3.0"

#: Written into every sidecar and log record, so frames trace back to the code that took them.
#: 0.2.0 (19 Nov 2026): PRD §8.7 storage (thumbs/, staging, hourly promotion), the archive
#: queue in the loop, one www.nps.gov back-off, live sidecar schema 2.
#: 0.2.1 (20 Nov 2026; never committed or run before these notes): both polite rules count
#: from the last request's *answer* (the lead's decision): a poll is sent no earlier than the
#: last poll's answer time + 300 s + 2 ms, and an image GET only while fewer than 4 of the
#: camera's GETs were answered in the last 3600 s (pacing from the last answer too), so they
#: hold at nps.gov whatever the network does. Each poll is reserved and saved before its send,
#: as each GET is; a live request is abandoned unsent past a 30 s send deadline, so an answer
#: a crash kept from the state is bounded (reservation + 60 s; state from 0.2.0: its send + 60 s
#: and no earlier than this code's first read). Within a process answers are also read on the
#: monotonic clock (wall-clock steps). A pass waits for slots only within its own window from
#: its start (Lambda 45 s, loop 60 s). ``request`` records gain ``sent_at`` and ``answered_at``,
#: ``archive_request`` records ``runner``; ``answer_bound`` records a settled bound. The
#: archive held-out guard also reads UTC dates (a 31 May local-evening frame is 1 Jun UTC), as
#: snow-globe-eval does; an hourly archive pick no longer files a frame after 23:30 under its own
#: date's 00:00. Formats additive: state gains ``last_answer_at``, ``last_answer_bound``,
#: ``poll_reserved_at``; GET records ``reserved_at``, ``answered_at``, ``answer_bound``.
#: 0.3.0 (21 Nov 2026): the rename from Lookout to Snow Globe (D-080), and nothing else. The
#: package, the import and the console script are ``snow-globe-capture``/``snow_globe_capture``,
#: and this stamp reads ``snow-globe-capture/0.3.0``; records, sidecars and state keep their
#: formats. Everything written before carries ``lookout-capture/<version>`` (0.1.0 to 0.2.1),
#: which every reader still accepts (:func:`capture_release`).
CAPTURE_NAME = "snow-globe-capture"
#: The name before the rename: the stamp of every record, sidecar and state written by 0.1.0
#: to 0.2.1.
LEGACY_CAPTURE_NAME = "lookout-capture"
CAPTURE_VERSION = f"{CAPTURE_NAME}/{__version__}"
#: Prefixes of a capture stamp, the current one first.
CAPTURE_VERSION_PREFIXES: tuple[str, ...] = (f"{CAPTURE_NAME}/", f"{LEGACY_CAPTURE_NAME}/")


def capture_release(stamp: object) -> str | None:
    """The version number of a capture stamp under either name (D-080), else None.

    ``"snow-globe-capture/0.3.0"`` and ``"lookout-capture/0.2.1"`` give ``"0.3.0"`` and
    ``"0.2.1"``, so readers compare releases, not spellings; anything else gives None.
    """
    if not isinstance(stamp, str):
        return None
    for prefix in CAPTURE_VERSION_PREFIXES:
        if stamp.startswith(prefix) and len(stamp) > len(prefix):
            return stamp[len(prefix) :]
    return None


__all__ = [
    "CAPTURE_NAME",
    "CAPTURE_VERSION",
    "CAPTURE_VERSION_PREFIXES",
    "LEGACY_CAPTURE_NAME",
    "__version__",
    "capture_release",
]
