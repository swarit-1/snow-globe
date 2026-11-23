# snow-globe-plates

The offline plate builder (PRD §8.5, §12). A plate set is what the renderer needs for one viewpoint: a base
frame, the camera's pose, and masks. Plates are data, so they are written to the data dir, never to the repo:
`<data dir>/plates/<viewpoint_id>/<plate_version>/`. Python 3.13, uv.

```sh
cd plates && uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
PYTHONPATH=src:../workers/common/src:../eval/src uv run snow-globe-plates match-stars \
  --spec specs/rainier.paradise-mountain.v0.1.json --catalogue <local copy of CDS V/50 catalog.gz>
PYTHONPATH=src:../workers/common/src:../eval/src uv run snow-globe-plates build --spec specs/rainier.paradise-mountain.v0.1.json
PYTHONPATH=src:../workers/common/src:../eval/src uv run snow-globe-plates build --spec specs/rainier.paradise-mountain.v0.1.json --dry-run
PYTHONPATH=src:../workers/common/src:../eval/src uv run snow-globe-plates candidates --viewpoint rainier.paradise-mountain
```

Outside pytest, iCloud's hidden `.pth` files mean the commands need
`PYTHONPATH=src:../workers/common/src:../eval/src` (CLAUDE.md "Python"). No command touches the network. The
data dir is snow-globe-common's `data_dir()` unless `--data-dir` is given. A plate folder or a star match file
that exists is never overwritten: a new build takes a new `plate_version`.

The package depends on snow-globe-eval (a path dependency) for one thing: the committed held-out split and its
guard, `snow_globe_eval.leakage.frame_file_verdict`. A plate is a fitted asset (PRD §13.9), so every input
frame must be one the split lets a **fit** use. The rule is not copied here.

## What a build does

The **spec** (`specs/<viewpoint>.<version>.json`, committed) holds everything decided by viewing the frames:
- the base-frame candidates, with the reason each was taken or left;
- the landmark marks and how each was identified;
- the night frames for the stars, the matcher's settings, and the SHA-256 of the match file;
- the occluder polygon, the band edges, the band registration bar, and the sources with their access dates.

It holds **no star catalogue positions**: `match-stars` writes those to the data dir (below).

The build recomputes everything else from the frames and the DEM, and checks each input's SHA-256:

| PRD §8.5 step | Here | Module |
|---|---|---|
| 1 Base frame | Median of the spec's frames. They must register with zero shift, and per-channel exposure gains are matched on static terrain. **Not dehazed**: TODO(M2), it needs §9.3's haze profile. | `base.py` |
| 3 Pose | Heading, pitch, roll and FOV, plus the camera's east/north position, from GNIS landmarks marked by viewing together with stars matched in night frames. | `joint.py`, `pose.py`, `stars.py`, `starmatch.py` |
| 6 Masks | Sky from a clear frame's blue-sky segmentation. Landmark bands from world height and distance in a USGS 3DEP DEM view through the solved pose, minus occluders, **clipped to the terrain the clear frame shows**, plus a 6 px sky margin grown from the observed skyline. No pixel is in two bands. The camera's caption strip. | `masks.py`, `skyline.py`, `dem.py` |
| 7 Pose check, part 1 | Each landmark's reprojection angle (PRD bar ≤ 1°), which is a **fit residual**: the camera position is fitted to the landmarks. The independent check is the DEM skyline against the observed one **per band, over every column the band owns**, against the spec's bar; a band that misses it is written but marked `valid: false` with the reason. The lens-height sensitivity of the solve. | `joint.py`, `skyline.py` |
| 2, 4, 5, 8, 9 | Night frame and lights mask; depth, world height, normals and horizon maps; sunset-shadow timing; camera response | TODO(M2) |

Inputs must be frames the committed split (`eval/src/snow_globe_eval/heldout_periods_v2.json`) lets a fit use
(`frames.py`):
- not in any held-out period (design P's sealed window, 15 Dec 2026 – 14 Jan 2027, with any outage extension
  the split records; design H's summer 2026 where the viewpoint has it);
- not an archived frame dated 1 Jun 2026 or later by any reading of its date;
- imaged before the viewpoint's fit bound (2026-12-15T08:00Z at Rainier; 2026-06-01T07:00Z at Point Reyes);
- not under `quarantine/`, in any letter case or through a link;
- and with a sidecar time within 2 s of the key's stamp.

It also refuses an output folder inside a git checkout.

### Stars

The manifest gives the camera position only to ±500 m: it assumes the Jackson Visitor Center. Every landmark
is 5–8 km away, so a wrong position is absorbed by a wrong heading and FOV. Fits from landmarks alone, or from
the DEM skyline alone, came out with FOVs between about 66° and 100°, depending on the position assumed. One of
them matched Little Tahoma to the wrong peak.

The infrared night frames show stars, and a star's direction doesn't depend on where the camera stands.
`snow-globe-plates match-stars` (`starmatch.py`) finds them:
- **catalogue:** the Bright Star Catalogue, 5th revised ed. (Hoffleit & Warren 1991, CDS V/50), read from the
  owner's local copy, with proper motions applied to each frame's epoch;
- **finding:** a 1 px blur minus a 15 px median, noise from the MAD of an empty-sky patch;
- **matching:** five passes (search radius 28 → 4 px, floor 10 → 6 sigma), the pose refitted robustly after
  each, then the matches under 4 px of the last pose kept;
- **output:** a match file in the data dir with every kept star, every match the cut rejected (with its
  residual), every star the last pass could not match (with the reason), and the residuals before and after
  the cut. The spec cites the file by SHA-256.

## Rainier, plate v0.1 (built 23 Nov 2026 UTC)

Plate v0.1 supersedes v0 (built earlier the same night and still in the data dir with its spec, under
`sources/superseded/`). The changes come from two reviews: the split's fit bound on inputs; the star positions
out of the repo and the matcher committed; bands clipped to the observed terrain with disjoint margins; the
per-band skyline check.

**Base frame:** the median of three bright-overcast frames of 21 Nov 2026, at 18:21:01Z, 18:37:01Z and
17:50:04Z. The Sun was at 17.7–21.0°, 76–123 min before solar noon, with every band visible.
- All 105 daytime captures (Sun ≥ 3°, 19–22 Nov) were viewed.
- Only 21 Nov 17:19–20:28Z offered overcast with the mountain out near midday.
- The spec lists 15 candidates with reasons. Six were rejected for a veiled summit dome, three for sun on the
  mountain, and the clear 20 Nov morning for direct light.
- The three frames registered with zero shift. Their exposure gains were 1.0, 0.93–0.95 and 0.98–0.99.

**Pose:**

| | Value |
|---|---|
| Heading, pitch, roll | 2.1370°, 9.9936°, 1.6426° |
| hfov | 68.5637° |
| Camera | 46.787833, −121.737037; lens 1697.95 m, assuming 10 m above the DEM ground (1688.0 m) |
| Camera offset from the manifest | 457 m: 275 m east and 365 m north |

The pose comes from three GNIS landmarks and 37 stars in 9 night frames of 20 Nov:

| Landmark (GNIS id) | Marked px | Fit residual |
|---|---|---|
| Gibraltar Rock (1533567) | 775, 329 | 0.28° |
| Anvil Rock (1533536) | 1159, 418 | 0.12° |
| Little Tahoma Peak (1533577) | 1283, 437 | 0.23° |

With three landmarks, leave-one-out cannot run (it needs four). A fourth was not added: the visible GNIS
points are either near-field (McClure Rock, Panorama Point), where the geometry is in question (below), or
ridges and cliffs with no point to mark (TODO(M2)).

**Checks:**

| Check | Result |
|---|---|
| Stars, kept 37 (joint fit) | median 1.62 px (0.059°), max 4.11 px (0.14°) |
| Stars, all 77 matches of the last pass, before the 4 px cut | median 4.57 px, p90 6.47 px, max 9.09 px; 40 rejected |
| Pose from the landmarks alone, at the solved position (not independent) | 2.02° / 9.74° / 1.41° / 69.62° |
| Landmark-only vs star-only pose, ray by ray | median 0.49°, max 0.84° |
| DEM skyline vs observed, summit band (256 columns, x 477–737) | median 0.16°, p90 0.41°, max 2.28° (tree edge): passes the 1° bar |
| upper band (323 columns, x 734–1316) | median 0.20°, p90 0.37°: passes |
| mid band (115 columns, x 1122–1404) | median 0.08°, p90 0.16°: passes |
| near band (306 columns, x 1325–1919) | median 0.33°, p90 1.22°, max 1.59°: **fails**, the band is `valid: false` |
| Lens height 3 / 10 / 20 / 40 m | camera 325 / 365 / 393 / 439 m north; landmark residual max 0.22 / 0.28 / 0.35 / 0.49°; landmark-only vs star-only max 0.62 / 0.84 / 1.04 / 1.41° |

**What the stars show.** The 40 matches the 4 px cut removes are real stars, not noise: many are bright and
isolated, several are the same star sitting 2 px from its prediction in one frame and 6 px in another. Neither
a per-frame aim, radial and tangential distortion, nor a principal-point offset brings the 77 under a median of
2.5 px. So the kept stars' residuals describe the half of the stars the pinhole fits, and the star orientation
is good to about 0.1–0.2° (2.5–5 px), not 0.06° (TODO(M2): the camera model).

**Near field.** The near ridge 1.8–3.3 km out (Panorama Point, The Sugarloaf, McClure Rock) is drawn about
35–50 px right of where the frame shows it. Its DEM skyline misses the observed one by p90 1.22°. Exploratory
re-solves (scratch, on plate v0's inputs, with alternate 40-column blocks of the near skyline held out):
- fitting the near skyline with the stars and landmarks fits the near ridge (held-out median 2.6 px) only from a
  camera about 170 m further south, which moves the far landmarks 10–17 px off their marks and the far skyline
  to a 7 px median;
- dropping the stars and freeing the lens height fits both skylines (held-out medians 2.5 and 2.4 px) with a
  lens 77 m above the DEM ground and an orientation that puts the stars 18–29 px off;
- weighting the stars at 5 px, their real scatter, gives a lens 35 m above the ground, the stars at a 6.1 px
  median, and the near ridge still at p90 13–21 px.

No single pinhole camera fits the stars, the far landmarks and the near ridge together, so the near band is
marked not valid for K2 until the camera position and model are resolved (TODO(M2)).

**Bands:**

| Band | Pixels | Definition | Valid |
|---|---|---|---|
| summit | 4,247 | ≥ 4,000 m | yes |
| upper | 33,231 | 3,200–4,000 m | yes |
| mid | 20,267 | 2,400–3,200 m | yes |
| near | 4,104 | < 2,400 m, 1.5–4 km out | no (skyline p90 1.22°) |

- Every band is clipped to the terrain the clear frame of 20 Nov 17:17Z shows; its sky margin grows from the
  observed skyline, and a margin pixel near two bands goes to the nearer one. `bands.png` and the per-band
  PNGs agree pixel for pixel.
- Columbia Crest itself is hidden from this camera: the dome's nearer slope rises above the line of sight from
  0.9 km in front of it, by up to 0.47°. Point Success is behind the big tree. P1's summit band is therefore the
  visible dome above 4,000 m.
- The near forest and the big tree are removed with the spec's occluder polygon, traced from dark pixels
  connected to the near terrain, then edited by viewing.

**Outputs:** `base.png`, `sky.png`, `bands.png` (labels 1–4), `band_<name>.png`, `occluder.png`,
`overlay.png` and `plate.json`. `plate.json` records:
- every input frame and its SHA-256, the star match file's SHA-256 and summary (with the rejected matches);
- the DEM and the spec, with their SHA-256s;
- the candidates, the pose solution, every check, and each band's registration and validity;
- the TODOs.

The DEM is a USGS 3DEP bare-earth subset (public domain), fetched once on 23 Nov 2026 with the
`exportImage` request recorded in `sources/provenance.json`. It sits beside the GNIS query response and the
star match file in `<data dir>/plates/rainier.paradise-mountain/sources/`.

## Tests

`uv run pytest -q`, on synthetic arrays only (no camera pixels and no catalogue data in the repo). They cover:
- the renderer's camera conventions, ported from `renderer/src/math/camera.test.ts`;
- geodesy, and sidereal time against Meeus 12.a;
- precession, refraction and star altitudes;
- the star matcher on a made-up sky: pose recovery, a displaced star rejected and recorded, the BSC5 byte
  layout and proper motion;
- pose recovery and a wrong mark caught by leave-one-out;
- the joint star and landmark solve;
- the DEM reader and ray cast on a synthetic cone;
- band masks (disjoint margins, clipping to the observed sky), the per-band skyline check, the median combine;
- the input guard: held-out periods, the fit bound (Rainier after the window, Point Reyes after 1 Jun 2026),
  the archive guard, quarantine through a parent step and through a linked folder in another case;
- an end-to-end build on a synthetic data dir: pose recovered, every file's SHA-256 matching `plate.json`, and
  refusals of a repo folder, a changed DEM, a changed match file, star positions in the spec, and an existing
  plate.
