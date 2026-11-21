from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from snow_globe_common import manifest as mf
from snow_globe_common.manifest import ManifestError

from .conftest import REPO_ROOT

MANIFESTS = REPO_ROOT / "viewpoints" / "manifests"
SCHEMA = REPO_ROOT / "viewpoints" / "schema" / "viewpoint.schema.json"

# CLAUDE.md "Viewpoint IDs" table, plus the task's zone and status assignments.
EXPECTED = {
    "rainier.paradise-mountain": ("America/Los_Angeles", "msv", None),
    "point-reyes.beach": ("America/Los_Angeles", "msv", "pore"),
    "grand-canyon.yavapai": ("America/Phoenix", "third", "grca"),
    "grand-teton.science-center": ("America/Denver", "v1-candidate", "grte"),
    "yosemite.turtleback": ("America/Los_Angeles", "v1-candidate", "yose"),
    "acadia.mcfarland": ("America/New_York", "v1-candidate", "acad"),
    "smokies.look-rock": ("America/New_York", "v1-candidate", "grsm"),
    "big-bend.panther-junction": ("America/Chicago", "v1-candidate", "bibe"),
}
ARCHIVE_SINCE_YEAR = {
    "pore": 2004, "grca": 2001, "grte": 2011, "yose": 2005,
    "acad": 2006, "grsm": 1998, "bibe": 2004,
}  # fmt: skip
FEED_AZIMUTH = {
    "point-reyes.beach": 22.5,
    "grand-canyon.yavapai": 315.0,
    "grand-teton.science-center": 270.0,
    "yosemite.turtleback": 225.0,
    "acadia.mcfarland": 45.0,
    "smokies.look-rock": 90.0,
    "big-bend.panther-junction": 0.0,
}


@pytest.fixture(scope="module")
def viewpoints() -> dict[str, mf.Viewpoint]:
    return mf.load_manifests(MANIFESTS)


def test_schema_is_valid_draft_2020_12():
    schema = mf.load_schema(SCHEMA)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_all_eight_viewpoints_load(viewpoints):
    assert set(viewpoints) == set(EXPECTED)
    for vid, (zone, status, code) in EXPECTED.items():
        vp = viewpoints[vid]
        assert (vp.time_zone, vp.status, vp.truth_camera.archive_code) == (zone, status, code)
        assert vp.zone.key == zone
        assert vp.place_id == vid.split(".")[0]
        assert vp.default is True
        assert len(vp.aliases) >= 6 and all(a == a.lower().strip() for a in vp.aliases)


def test_feed_urls_and_archive(viewpoints):
    for vid, vp in viewpoints.items():
        truth = vp.truth_camera
        if vid == "rainier.paradise-mountain":
            assert truth.kind == "park-cam"
            assert truth.image_url == "https://www.nps.gov/webcams-mora/mountain.jpg"
            assert truth.json_url is None and truth.archive_since is None and not vp.has_archive
            continue
        code = truth.archive_code
        assert truth.kind == "ard-aircam"
        assert truth.json_url == (
            f"https://www.nps.gov/featurecontent/ard/webcams/json/{code}json.txt"
        )
        assert truth.image_url == (
            f"https://www.nps.gov/featurecontent/ard/webcams/images/{code}large.jpg"
        )
        assert truth.archive_since is not None
        assert truth.archive_since.year == ARCHIVE_SINCE_YEAR[code]
        assert truth.interval_min_nominal == 15
        assert truth.image_px == (3840, 2160) and truth.interval_min_measured is None


def test_licence_and_polite_fields(viewpoints):
    for vp in viewpoints.values():
        truth = vp.truth_camera
        assert truth.polite_caps == mf.PoliteCaps(4, 300, True)
        assert truth.publish_cleared is False and truth.confirmation == "to-request"
        assert truth.people_in_frame is True and truth.fixed_view is True
        assert truth.terms_url == "https://www.nps.gov/aboutus/disclaimer.htm"
        assert truth.terms_checked.isoformat() == "2026-11-19"
        assert vp.attribution == (
            "Webcam imagery courtesy National Park Service. Derived images are not NPS products."
        )


def test_view_azimuths(viewpoints):
    for vid, azimuth in FEED_AZIMUTH.items():
        assert viewpoints[vid].camera.view_azimuth_deg == azimuth


def test_rainier_summit_landmark(viewpoints):
    vp = viewpoints["rainier.paradise-mountain"]
    summit = vp.landmark("Mount Rainier summit (Columbia Crest)")
    assert summit.band == "summit" and summit.px is None
    assert summit.height_m == 4392
    assert (summit.lat, summit.lon) == pytest.approx((46.8529, -121.7604), abs=1e-3)
    cam = vp.camera
    distance = mf.great_circle_km(cam.lat, cam.lon, summit.lat, summit.lon)
    bearing = mf.initial_bearing_deg(cam.lat, cam.lon, summit.lat, summit.lon)
    assert summit.distance_km == pytest.approx(distance, abs=0.01)
    assert cam.view_azimuth_deg == pytest.approx(bearing, abs=0.06)
    with pytest.raises(KeyError):
        vp.landmark("Half Dome")


def test_alias_hazards(viewpoints):
    every = {a for vp in viewpoints.values() for a in vp.aliases}
    assert "big ben" not in every  # London, not Big Bend
    assert {"point rays", "mount rainer", "the mountain"} <= every


def test_load_manifest_and_env_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    assert mf.load_manifest("point-reyes.beach", MANIFESTS).truth_camera.archive_code == "pore"
    with pytest.raises(KeyError):
        mf.load_manifest("golden-gate.bridge", MANIFESTS)
    monkeypatch.setenv("SNOW_GLOBE_MANIFEST_DIR", str(MANIFESTS))
    assert mf.manifest_dir() == MANIFESTS.resolve()
    assert mf.schema_path() == SCHEMA.resolve()
    monkeypatch.setenv("SNOW_GLOBE_MANIFEST_DIR", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        mf.manifest_dir()


def test_discovery_walks_up_without_env():
    assert mf.manifest_dir() == MANIFESTS.resolve()


# --- negative cases on a scratch copy -------------------------------------------------------
@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    root = tmp_path / "viewpoints"
    shutil.copytree(REPO_ROOT / "viewpoints", root)
    return root / "manifests"


def _edit(folder: Path, vid: str, change) -> None:
    path = folder / f"{vid}.json"
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def _set(path: list[str], value):
    def change(doc):
        target = doc
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return change


@pytest.mark.parametrize(
    ("vid", "change", "message"),
    [
        ("point-reyes.beach", lambda d: d.pop("attribution"), "attribution"),
        ("point-reyes.beach", _set(["aliases"], ["a", "b", "c"]), "aliases"),
        ("point-reyes.beach", _set(["aliases"], ["Point Reyes"] * 6), "aliases"),
        ("point-reyes.beach", _set(["status"], "hero"), "status"),
        ("point-reyes.beach", _set(["unexpected"], 1), "unexpected"),
        ("point-reyes.beach", _set(["truth_camera", "polite_caps", "max_images_per_hour"], 5), "4"),
        ("point-reyes.beach", _set(["truth_camera", "polite_caps", "poll_interval_s"], 60), "300"),
        ("point-reyes.beach", _set(["truth_camera", "publish_cleared"], True), "confirmed"),
        ("point-reyes.beach", _set(["truth_camera", "kind"], "pixelcaster"), "kind"),
        ("point-reyes.beach", _set(["truth_camera", "image_url"], "http://x/y.jpg"), "image_url"),
        ("point-reyes.beach", _set(["truth_camera", "archive_code"], "PORE"), "archive_code"),
        ("point-reyes.beach", _set(["truth_camera", "terms_checked"], "2026-13-40"), "date"),
        ("point-reyes.beach", _set(["camera", "view_azimuth_deg"], 360), "view_azimuth_deg"),
        ("point-reyes.beach", _set(["camera", "sources"], []), "sources"),
        ("point-reyes.beach", _set(["time_zone"], "America/Atlantis"), "IANA"),
        ("point-reyes.beach", _set(["place_id"], "pore"), "prefix"),
        ("point-reyes.beach", _set(["id"], "point-reyes.lighthouse"), "file name"),
        (
            "acadia.mcfarland",
            _set(["aliases"], ["acadia", "a2", "a3", "a4", "a5", "point rays"]),
            "both",
        ),
        (
            "rainier.paradise-mountain",
            lambda d: d["landmarks"][0].__setitem__("distance_km", 12.0),
            "great-circle",
        ),
        # Review findings: cross-field rules the loader used to miss.
        (
            "rainier.paradise-mountain",
            lambda d: d["landmarks"][0].__setitem__("azimuth_deg", 10.0),
            "initial bearing from the camera is 348.78",
        ),
        (
            "point-reyes.beach",
            _set(["truth_camera", "archive_code"], None),
            "both be set or both be null",
        ),
        (
            "point-reyes.beach",
            _set(["truth_camera", "archive_since"], None),
            "both be set or both be null",
        ),
        ("point-reyes.beach", lambda d: d["aliases"].append("point  reyes"), "aliases"),
        ("point-reyes.beach", lambda d: d["aliases"].append("point reyes "), "aliases"),
        ("acadia.mcfarland", lambda d: d["aliases"].append("point-reyes"), "as 'point reyes'"),
        ("acadia.mcfarland", lambda d: d["aliases"].append("point rey's"), "as 'point reys'"),
        # The PRD Appendix A shape, refused with the field's new home.
        (
            "rainier.paradise-mountain",
            _set(["plate"], {"version": 1, "landmarks": [{"name": "summit"}]}),
            "plate.landmarks is not used: put it in the top-level landmarks",
        ),
        ("rainier.paradise-mountain", _set(["camera", "heading_deg"], 348.8), "pose.heading_deg"),
        (
            "point-reyes.beach",
            _set(["truth_camera", "polite_caps", "max_per_hour"], 4),
            "polite_caps.max_images_per_hour",
        ),
        ("point-reyes.beach", _set(["plate"], {"version": "one"}), "plate/version"),
        ("point-reyes.beach", _set(["plate"], {"sky_mask": ""}), "plate/sky_mask"),
    ],
)
def test_bad_manifests_are_rejected(scratch: Path, vid: str, change, message: str):
    _edit(scratch, vid, change)
    with pytest.raises(ManifestError, match=message):
        mf.load_manifests(scratch)


def test_two_defaults_for_one_place_are_rejected(scratch: Path):
    doc = json.loads((scratch / "point-reyes.beach.json").read_text())
    twin = copy.deepcopy(doc)
    twin["id"] = "point-reyes.lighthouse"
    (scratch / "point-reyes.lighthouse.json").write_text(json.dumps(twin))
    with pytest.raises(ManifestError, match="exactly one default"):
        mf.load_manifests(scratch)
    twin["default"] = False
    (scratch / "point-reyes.lighthouse.json").write_text(json.dumps(twin))
    assert "point-reyes.lighthouse" in mf.load_manifests(scratch)  # same place may share aliases


def test_invalid_json_is_reported_with_the_file_name(scratch: Path):
    (scratch / "point-reyes.beach.json").write_text("{")
    with pytest.raises(ManifestError, match=r"point-reyes\.beach\.json"):
        mf.load_manifests(scratch)


def test_m2_fields_are_optional_and_nullable(scratch: Path):
    def add(doc):
        doc["pose"] = None
        doc["plate"] = {"version": 1}
        doc["haze_profile"] = None
        doc["camera_response"] = None
        doc["forecast_samples"] = [
            {"layer": "low", "height_m": 1500, "lat": 38.0, "lon": -123.0, "grid_cell": [1, 2]}
        ]

    _edit(scratch, "point-reyes.beach", add)
    vp = mf.load_manifests(scratch)["point-reyes.beach"]
    assert vp.raw["plate"] == {"version": 1}


def test_appendix_a_plate_keys_validate(scratch: Path):
    def add(doc):
        doc["plate"] = {
            "version": 1,
            "base": "base.webp",
            "sky_mask": "sky.png",
            "dem": {"source": "USGS 3DEP 1/3 arc-second", "licence": "public domain", "datum": ""},
            "assets_licence": {"base": "NPS public domain (pending confirmation)"},
        }

    _edit(scratch, "rainier.paradise-mountain", add)
    assert mf.load_manifests(scratch)["rainier.paradise-mountain"].raw["plate"]["version"] == 1


def test_landmark_azimuth_within_half_a_degree_is_accepted(scratch: Path):
    _edit(
        scratch,
        "rainier.paradise-mountain",
        lambda d: d["landmarks"][0].__setitem__("azimuth_deg", 349.2),  # 0.42 deg off
    )
    assert mf.load_manifests(scratch)["rainier.paradise-mountain"].landmarks


def test_normalise_alias():
    assert mf.normalise_alias("Point-Reyes") == "point reyes"
    assert mf.normalise_alias("point  rey's") == "point reys"
    assert mf.normalise_alias("mount rainier") == "mount rainier"


# Factual correction (2026-11-19 capture-loop sidecars; see each manifest's notes).
NIGHT_MODE = {
    "rainier.paradise-mountain": "infrared",
    "point-reyes.beach": "infrared",
    "grand-canyon.yavapai": "infrared",
    "grand-teton.science-center": "infrared",
    "yosemite.turtleback": "infrared",
    "smokies.look-rock": "infrared",
    "big-bend.panther-junction": "infrared",
    "acadia.mcfarland": "unknown",  # its only dark frame was in nautical twilight, in colour
}


def test_night_modes(viewpoints):
    assert {vid: vp.truth_camera.night_mode for vid, vp in viewpoints.items()} == NIGHT_MODE
    for vid, vp in viewpoints.items():
        if NIGHT_MODE[vid] == "infrared":
            assert any("chroma_spread" in note for note in vp.raw["notes"]), vid


def test_geodesy_helpers():
    assert mf.great_circle_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)
    assert mf.initial_bearing_deg(0, 0, 1, 0) == pytest.approx(0.0)
    assert mf.initial_bearing_deg(0, 0, 0, 1) == pytest.approx(90.0)
    assert mf.initial_bearing_deg(0, 0, -1, 0) == pytest.approx(180.0)
    assert mf.initial_bearing_deg(0, 0, 0, -1) == pytest.approx(270.0)


def test_cli_reports_ok_and_failure(capsys, tmp_path: Path):
    assert mf.main([str(MANIFESTS)]) == 0
    assert "8 valid" in capsys.readouterr().out
    assert mf.main([str(tmp_path / "missing")]) == 1
    assert "FAILED" in capsys.readouterr().err
