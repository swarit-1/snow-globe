"""The v−1 registration diagnostic (PRD §8.6 "Same view"): known shifts are recovered."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from snow_globe_eval.registration import estimate_shift


def _texture(width: int = 720, height: int = 480, seed: int = 7) -> np.ndarray:
    """A smooth random texture with edges at many scales (a stand-in for canyon walls)."""
    rng = np.random.default_rng(seed)
    base = rng.random((height // 8 + 2, width // 8 + 2))
    big = np.kron(base, np.ones((8, 8)))[:height, :width]
    fine = rng.random((height, width)) * 0.2
    return np.clip(0.2 + 0.6 * big + fine, 0, 1)


def _image(array: np.ndarray) -> Image.Image:
    return Image.fromarray((array * 255).astype(np.uint8), "L").convert("RGB")


def _shifted(array: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Content moved right by dx and down by dy (edges filled by wrapping)."""
    return np.roll(np.roll(array, dy, axis=0), dx, axis=1)


def test_identity_is_zero() -> None:
    plate = _image(_texture())
    shift = estimate_shift(plate, plate, window=(0.1, 0.1, 0.9, 0.9), analysis_width=360)
    assert (shift.dx_px, shift.dy_px, shift.scale) == (0.0, 0.0, 1.0)
    assert shift.ncc == pytest.approx(1.0) and shift.ncc_unshifted == pytest.approx(1.0)


@pytest.mark.parametrize(("dx", "dy"), [(36, -22), (-50, 14), (8, 30)])
def test_a_known_shift_is_recovered(dx: int, dy: int) -> None:
    texture = _texture()
    plate, frame = _image(texture), _image(_shifted(texture, dx, dy))
    shift = estimate_shift(plate, frame, window=(0.15, 0.15, 0.85, 0.85), analysis_width=360)
    per_px = 720 / 360  # one analysis pixel is two frame pixels
    assert abs(shift.dx_px - dx) <= per_px and abs(shift.dy_px - dy) <= per_px
    assert shift.dx_norm == pytest.approx(dx / 720, abs=1 / 360)
    assert shift.ncc > shift.ncc_unshifted + 0.3
    assert shift.to_dict()["method"].startswith("phase correlation")


def test_a_change_of_light_does_not_move_the_estimate() -> None:
    texture = _texture()
    frame = 0.15 + 0.5 * _shifted(texture, 20, -12)  # darker, lower contrast
    shift = estimate_shift(
        _image(texture), _image(frame), window=(0.15, 0.15, 0.85, 0.85), analysis_width=360
    )
    assert abs(shift.dx_px - 20) <= 2 and abs(shift.dy_px + 12) <= 2


def test_bad_inputs_are_refused() -> None:
    plate = _image(_texture())
    with pytest.raises(ValueError, match="aspect"):
        estimate_shift(plate, _image(_texture(720, 300)))
    with pytest.raises(ValueError, match="window must"):
        estimate_shift(plate, plate, window=(0.5, 0.5, 0.4, 0.9))
    with pytest.raises(ValueError, match="too small"):
        estimate_shift(plate, plate, window=(0.5, 0.5, 0.51, 0.51), analysis_width=360)
