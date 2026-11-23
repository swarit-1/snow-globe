"""The star matcher (plates.starmatch) on a synthetic sky: made-up stars, no catalogue data."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from snow_globe_plates.camera import Pose, project
from snow_globe_plates.starmatch import (
    Catalogue,
    MatchSettings,
    NightFrame,
    match_stars,
    read_bsc5,
    summary,
)
from snow_globe_plates.stars import apparent_alt_az

W, H = 640, 360
TRUE = Pose(2.0, 30.0, 1.0, 60.0, W, H)
CAMERA = (46.78, -121.74)
T = datetime(2026, 11, 20, 11, 0, 4, tzinfo=UTC)
SETTINGS = MatchSettings(
    vmax=5.0,
    passes=((20, 8.0), (8, 6.0), (4, 6.0)),
    search_box=(10, 0, W - 10, H - 10),
    noise_box=(0, 0, W, 60),
    sky_margin_px=4,
)


def _directions(cat: Catalogue) -> np.ndarray:
    ra, dec = cat.at(T)
    az, alt = apparent_alt_az(ra, dec, T, *CAMERA)
    a, h = np.radians(az), np.radians(alt)
    return np.stack([np.sin(a) * np.cos(h), np.cos(a) * np.cos(h), np.sin(h)], axis=-1)


def _sky() -> tuple[Catalogue, np.ndarray]:
    """A grid of made-up stars, and the pixels where the true pose puts the visible ones."""
    ra, dec = np.meshgrid(np.arange(0.0, 360.0, 12.0), np.arange(30.0, 80.0, 7.0))
    n = ra.size
    cat = Catalogue(
        np.arange(1, n + 1),
        ra.ravel(),
        dec.ravel(),
        np.zeros(n),
        np.zeros(n),
        np.linspace(1.0, 4.5, n),
        2000.0,
        "synthetic",
        "0" * 64,
    )
    uv, depth = project(_directions(cat), TRUE)
    return cat, np.where((depth > 0)[:, None], uv, np.nan)


def _frame(points: np.ndarray, extra: list[tuple[float, float]]) -> NightFrame:
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:H, 0:W]
    luma = 30.0 + rng.normal(0.0, 1.0, (H, W))
    for x, y in [*points.tolist(), *extra]:
        luma += 120.0 * np.exp(-((xx + 0.5 - x) ** 2 + (yy + 0.5 - y) ** 2) / (2 * 1.2**2))
    return NightFrame.from_luma(
        "frames/v/2026-11-20/x.jpg", "a" * 64, T, luma, noise_box=(0, 0, W, 60)
    )


def test_matcher_recovers_the_pose_and_records_what_it_rejected() -> None:
    cat, uv = _sky()
    inside = np.isfinite(uv[:, 0]) & (uv[:, 0] > -20) & (uv[:, 0] < W + 20) & (uv[:, 1] > -20)
    drawn_ids = np.nonzero(inside & (uv[:, 1] < H + 20))[0]
    drawn = uv[drawn_ids].copy()
    middle = np.nonzero(
        (uv[drawn_ids, 0] > 200)
        & (uv[drawn_ids, 0] < 440)
        & (uv[drawn_ids, 1] > 100)
        & (uv[drawn_ids, 1] < 250)
    )[0]
    assert len(drawn_ids) >= 15 and len(middle) >= 1
    moved = int(drawn_ids[middle[0]])
    drawn[middle[0]] += (5.0, 5.0)  # one star drawn 7 px off: a residual the model lacks
    start = Pose(2.4, 29.8, 0.7, 60.6, W, H)
    match = match_stars([_frame(drawn, [])], cat, CAMERA, start, np.full(W, 300.0), SETTINGS)
    assert match.pose.heading_deg == pytest.approx(TRUE.heading_deg, abs=0.02)
    assert match.pose.hfov_deg == pytest.approx(TRUE.hfov_deg, abs=0.03)
    assert [r["catalogue_id"] for r in match.rejected] == [int(cat.ids[moved])]
    assert match.rejected[0]["residual_px"] == pytest.approx(7.07, abs=0.6)
    assert max(match.residuals_after_cut_px) < 0.5
    assert max(match.residuals_before_cut_px) > 6.0  # the cut is recorded, not hidden
    info = summary(match)
    assert info["kept"] >= 10 and info["rejected_after_last_pass"] == 1
    assert info["residual_px_before_cut"]["max"] > 6.0


def test_stars_below_the_skyline_or_too_faint_are_not_matched() -> None:
    cat, uv = _sky()
    match = match_stars(
        [_frame(uv[np.isfinite(uv[:, 0])], [])],
        cat,
        CAMERA,
        TRUE,
        np.full(W, 150.0),  # a high skyline: only the top of the frame is sky
        SETTINGS,
    )
    assert all(k["px"][1] < 150 - SETTINGS.sky_margin_px + 1 for k in match.kept)
    with pytest.raises(ValueError, match="only"):
        match_stars([_frame(np.zeros((0, 2)), [])], cat, CAMERA, TRUE, np.full(W, 300.0), SETTINGS)


def _bsc5_line(
    hr: int,
    ra: tuple[int, int, float],
    dec: tuple[str, int, int, int],
    v: float,
    pm: tuple[float, float],
) -> str:
    line = [" "] * 197

    def put(start: int, text: str) -> None:
        line[start - 1 : start - 1 + len(text)] = list(text)

    put(1, f"{hr:4d}")
    put(76, f"{ra[0]:02d}{ra[1]:02d}{ra[2]:04.1f}")
    put(84, f"{dec[0]}{dec[1]:02d}{dec[2]:02d}{dec[3]:02d}")
    put(103, f"{v:5.2f}")
    put(149, f"{pm[0]:+6.3f}{pm[1]:+6.3f}")
    return "".join(line)


def test_read_bsc5_layout_and_proper_motion(tmp_path: Path) -> None:
    text = "\n".join(
        [
            _bsc5_line(1, (12, 30, 0.0), ("+", 45, 30, 0), 2.5, (0.0, 0.0)),
            _bsc5_line(2, (6, 0, 36.0), ("-", 10, 0, 36), 4.0, (0.36, -0.72)),
            " " * 10,  # a short line (a nova kept for numbering): skipped
        ]
    )
    path = tmp_path / "catalog.gz"
    path.write_bytes(gzip.compress(text.encode("latin-1")))
    cat = read_bsc5(path)
    assert cat.ids.tolist() == [1, 2]
    assert cat.ra_deg[0] == pytest.approx(187.5) and cat.dec_deg[0] == pytest.approx(45.5)
    assert cat.ra_deg[1] == pytest.approx(90.15) and cat.dec_deg[1] == pytest.approx(-10.01)
    ra, dec = cat.at(datetime(2100, 1, 1, 12, tzinfo=UTC))  # one Julian century on
    assert dec[1] - cat.dec_deg[1] == pytest.approx(-0.72 * 100 / 3600, rel=1e-3)
    expected = 0.36 * 100 / 3600 / np.cos(np.radians(cat.dec_deg[1]))
    assert ra[1] - cat.ra_deg[1] == pytest.approx(expected, rel=1e-3)
    assert cat.brighter_than(3.0).ids.tolist() == [1]
    empty = tmp_path / "empty.txt"
    empty.write_text("nothing here\n")
    with pytest.raises(ValueError, match="no catalogue entries"):
        read_bsc5(empty)
