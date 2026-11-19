# DECISIONS.md

Every model, vendor, data-source and architecture decision, with the evidence behind it. Entries are append-only; a reversed decision gets a new entry that points back.

**Status:** **decided** (evidence exists) · **proposed** (awaiting owner go-ahead) · **open** (to be measured).

---

## D-001 — Milestone 0 is documents only · decided · 2026-11-18
- PRD.md, PLAN.md, this file, `docs/research/` and `docs/council/`. No application code until the owner's go-ahead.

## D-002 — Ephemeris: reuse oculus-core · proposed · 2026-11-18
- **Decision:** Cargo git dependency on `oculus-core` (github.com/swarit-1/oculus, `core/`, MIT), pinned to `06809e6`.
  - `ephem::sun_moon` is used natively (solar events in the parser), via the `wasm` feature inside the renderer, and via the `python` feature in eval.
  - The MIT notice ships in the WASM bundle.
- **Evidence** (2026-11-18, `cargo test --release --test golden_astro`, 3/3 pass): worst error vs JPL Horizons was Sun 0.19′ and Moon 0.13′ (294 rows, 3 sites, 1950–2050), against a 30′ budget.
- **Caveats carried into M2:**
  - `Apparent.alt` is unrefracted, so we validate and render refracted altitude.
  - Horizons vectors are regenerated for each viewpoint.
  - Valid to 2050.
- **Rejected:** copying the ~1,500 lines, because fixes would fork.

## D-003 — Tier 1 renderer API: WebGL2 · decided (owner default) · 2026-11-18
- WebGL2 / GLSL ES 3.0. It runs in WKWebView and headless in Chromium/SwiftShader, which gives deterministic CPU renders for golden images and eval.

## D-004 — Speech-to-text · open (shortlist set) · 2026-11-19
- **Benchmark first:** A1 = SpeechAnalyzer `DictationTranscriber`; D = Deepgram Nova-3 with `Finalize`.
- **P = FluidAudio Parakeet** only if A1's *idle* release → final p95 exceeds 250 ms.
- **Soniox stt-rt-v6 in reserve** (no audio stored, no training) if Deepgram's opt-out or token flow fails.
- **Excluded:**
  - AssemblyAI, because free accounts can't opt out of training;
  - SpeechTranscriber, because it ignores contextual strings;
  - Deepgram Flux and the re-decode race, unless measurement calls for them.
- **Selection rule:** lowest idle p95 release → final at equal or better post-parser place accuracy. Fallback = the best engine of the other kind.
- **Sources:** `docs/research/stt-ondevice-macos.md`, `docs/research/stt-cloud.md`.

## D-005 — LLM fallback parser · open · 2026-11-19
- Apple Foundation Models in-process (`@Generable`), $0, no text leaves the machine.
- Claude Haiku 5.5 with thinking disabled, only if FM is unavailable or fails accuracy, and only after Anthropic's retention and training terms are recorded here.
- Last-but-one in the cut order.
- **Sources:** `docs/research/stt-ondevice-macos.md` §6, `docs/research/stt-cloud.md`.

## D-006 — Tier 2 refinement · open · 2026-11-19
- **Kill test first** (≤ $1): Z-Image-Turbo img2img on fal.
- **If it passes:** benchmark against Runware's host.
- **FLUX.2 klein 4B** on fal as a ~20-call illustration only (not BFL, whose privacy policy says it "may train on" inputs).
- **Composition:** sky locked to Tier 1; only transmittance-weighted ground detail transferred.
- **Source:** `docs/research/refinement.md`.

## D-007 — Viewpoints and cameras · proposed (owner input needed) · 2026-11-19
- **Viewpoints:**
  - Rainier park cam `nps.gov/webcams-mora/mountain.jpg` (1920×1080, Axis P1387; ownership metadata absent; cadence to be measured);
  - Point Reyes NPS air cam (3840×2160, 15 min);
  - Grand Canyon Yavapai air cam (3840×2160, 15 min; built after the MSV).
- **Golden Gate:** only with written permission by 4 Dec 2026.
- **Spot check 2026-11-19 04:20 UTC:** air-cam EXIF has `Artist: National Park Service`, `Copyright: National Park Service`, `Software: ARS_Digicam`. The Rainier air cam is only 512×288 and looks WSW (not at the summit), so it was rejected in favour of the park cam.
- **Publishing gated on NPS written confirmation** (PRD §3.5).
- **Source:** `docs/research/cameras.md`.

## D-008 — Forecast source and issue time · proposed · 2026-11-19
- **Primary:** HRRR direct from AWS `noaa-hrrr-bdp-pds` via Herbie byte ranges.
- **Open-Meteo:** pinned GFS beyond 48 h and for unverified places, bracketed by `meta.json`.
- **Why:**
  - Open-Meteo discontinues HRRR on 2026-11-30 (confirmed on its docs page, 2026-11-19).
  - It has no ceiling or cloud-base field.
  - Its responses carry no run time.
- **Issue time:**
  - Live runs: S3 `LastModified`.
  - Hindcast runs: run start + measured p95 lag per forecast hour.
  - Lead time = valid − availability.
- **Source:** `docs/research/forecast.md`.

## D-009 — Hosting · open (owner input needed)
- **Proposed:**
  - public repo;
  - GitHub Actions for the worker and batch jobs;
  - Cloudflare R2 for private frames;
  - Cloudflare Workers for the token minter or proxy (no scale-to-zero cold start), gated at ≤ 100 ms added p95.

## D-010 — Sky model · proposed · 2026-11-19
- Hosek–Wilkie ported from `andrewwillmott/sun-sky` (repo Unlicense; **data tables BSD-3**, notice kept), used for Sun ≥ 0°.
- Twilight from a separate model.
- Fallback: three.js `Sky.js` pinned at r187.

## D-011 — Evaluation design · proposed (frozen at the pre-registration commit) · 2026-11-19
- **Primary endpoint:** P1 = Rainier summit visible at 24 h, scored by ΔPSS vs the stronger persistence baseline, with a block bootstrap.
- **Renderer claims** R1–R3 as in PRD §8.2.
- **Design** H or P, chosen by the NPS archive check in M0.5.
- **Pre-registration hash:** to be recorded here before the held-out period starts.

---

## Spend ledger

| Date | Vendor | What | Amount | Running total |
|---|---|---|---|---|
| 2026-11-18/19 | — | M0 research and spot checks used only free, keyless public endpoints | $0.00 | $0.00 |
