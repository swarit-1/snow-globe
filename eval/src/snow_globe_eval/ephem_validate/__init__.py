"""Ephemeris validation (PRD §8.4 "Checks"; PLAN.md M2 "Ephemeris validation").

Measures oculus-core, through eval's binding (:mod:`snow_globe_eval.ephemeris`, the same core
``app/core`` uses), and the two things Snow Globe adds on top of it (PRD §8.4 "Additions"):

- the refracted apparent altitude (Sæmundsson, 1010 hPa, 10 °C);
- the transform into each viewpoint's camera frame (the renderer's pinhole camera: a pixel
  position and a light direction).

Against:

- JPL Horizons observer tables per viewpoint, hourly for 12 months (:mod:`.horizons`);
- 1,000 random place-and-time samples against Skyfield with JPL's DE421, run in an isolated
  environment because Skyfield is not a dependency of eval (:mod:`.skyfield_run`,
  :mod:`.skyfield_ref`);
- sunrise and sunset against NOAA to ±1 min (:mod:`.sunrise`).

Targets (PRD §8.4): Sun ≤ 0.05°, Moon ≤ 0.3°. :mod:`.run` puts it together; the CLI is
``snow-globe-eval ephem-validate`` (:mod:`.cli`).
"""
