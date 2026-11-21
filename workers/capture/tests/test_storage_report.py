"""The storage measurement: bytes per frame from disk, thumbnails made in memory, the new
sidecars' real sizes, and bytes per day under the PRD §8.7 policy against the old one."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from snow_globe_common.manifest import load_manifests
from snow_globe_common.storage import LocalStorage

from snow_globe_capture.cli import main
from snow_globe_capture.storage_report import daylight_share, storage_report

from .conftest import make_jpeg

VPS = load_manifests()
GRCA = VPS["grand-canyon.yavapai"]
RAINIER = VPS["rainier.paradise-mountain"]
DAY = date(2026, 11, 19)


def put_frame(
    storage: LocalStorage, vp, when: datetime, *, daylight: bool | None, stopgap=False, pattern=0
) -> int:
    body = make_jpeg(1920, 1080, pattern=pattern, quality=92)
    sha = hashlib.sha256(body).hexdigest()
    key = f"frames/{vp.id}/{when:%Y-%m-%d}/{when:%Y%m%dT%H%M%SZ}_{sha[:12]}.jpg"
    storage.put_bytes(key, body)
    doc = {"viewpoint_id": vp.id, "image_time_utc": when.isoformat(), "sha256": sha}
    if stopgap:
        doc["capture_version"] = "stopgap-0"
    else:
        doc.update(
            capture_version="lookout-capture/0.1.0",
            stored="original",
            stored_width=1920,
            stored_height=1080,
            stored_bytes=len(body),
            stored_sha256=sha,
            jpeg_quality=None,
            sun={"daylight": daylight},
            flags=[],
            exif={"Artist": "National Park Service"},
        )
    storage.write_json(key[:-4] + ".json", doc)
    return len(body)


def test_daylight_share_follows_the_ephemeris():
    share = daylight_share(GRCA, DAY)  # 36 N in mid-November: about 10 h above 3 degrees
    assert 0.36 <= share <= 0.45
    assert daylight_share(GRCA, date(2026, 6, 21)) > 0.55


def test_report_measures_frames_and_projects_both_policies(data_root):
    storage = LocalStorage(data_root)
    sizes = [
        put_frame(storage, GRCA, datetime(2026, 11, 19, h, 0, tzinfo=UTC), daylight=day, pattern=h)
        for h, day in ((10, False), (11, False), (18, True), (19, True))
    ]
    put_frame(
        storage, RAINIER, datetime(2026, 11, 19, 7, 0, tzinfo=UTC), daylight=None, stopgap=True
    )
    put_frame(storage, RAINIER, datetime(2026, 11, 19, 20, 0, tzinfo=UTC), daylight=True, pattern=5)
    report = storage_report(storage, [GRCA, RAINIER], day=DAY)
    rows = {r["viewpoint_id"]: r for r in report["cameras"]}
    grca, rainier = rows[GRCA.id], rows[RAINIER.id]
    assert grca["frames_on_disk"] == 4 and grca["frame_bytes"]["mean"] == pytest.approx(
        sum(sizes) / 4, abs=0.1
    )
    assert grca["frames_sun_up"] == 2 and grca["frames_sun_down"] == 2
    assert rainier["frames_sun_down"] == 1  # the stopgap frame's Sun comes from the ephemeris
    thumbs = grca["thumb_bytes_made_in_memory"]
    assert 0 < thumbs["mean"] < grca["frame_bytes"]["mean"] / 3
    sides = grca["sidecar_bytes"]["new"]
    assert sides["thumb_sidecar"] > sides["frame_sidecar"] > grca["sidecar_bytes"]["capture_mean"]
    assert sides["promoted_sidecar"] > sides["frame_sidecar"]
    hourly, every = grca["projection"], rainier["projection"]
    assert hourly["downloads_per_day"] == 96 and grca["frame_policy"] == "hourly-nearest"
    assert hourly["new_bytes_per_day"] < hourly["old_bytes_per_day"]
    assert hourly["new_full_frames_bytes_per_day"] == pytest.approx(
        24 * (hourly["frame_bytes_weighted"] + sides["promoted_sidecar"]), abs=24
    )  # frame_bytes_weighted is rounded to whole bytes
    assert (
        rainier["frame_policy"] == "every"
        and every["new_bytes_per_day"] > every["old_bytes_per_day"]
    )
    totals = report["totals_per_day"]
    assert totals["old_bytes"] == hourly["old_bytes_per_day"] + every["old_bytes_per_day"]
    assert totals["new_bytes_90_days"] == pytest.approx(90 * totals["new_bytes"], rel=1e-6)
    assert report["frames_span_utc"][0].startswith("2026-11-19T07:00")
    # Nothing was written: no thumbnails, no staging.
    assert not storage.list_prefix("thumbs/") and not storage.list_prefix("cache/")


def test_storage_command_runs_without_network(data_root, capsys):
    storage = LocalStorage(data_root)
    put_frame(storage, GRCA, datetime(2026, 11, 19, 18, 0, tzinfo=UTC), daylight=True)
    assert main(["storage", "--only", GRCA.id, "--day", "2026-11-19", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cameras"][0]["frames_on_disk"] == 1
    assert main(["storage", "--only", GRCA.id]) == 0
    assert "totals per day" in capsys.readouterr().out
