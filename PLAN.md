# PLAN.md — Milestones, dates, cut order and evidence

Companion to [PRD.md](PRD.md) v0.3. Each milestone stops for review. **A milestone is done when its evidence exists, not when the code compiles.**

**Dates.** Written as offsets from go-ahead day **G**. They become calendar dates once the owner gives hours/week (PRD §13 Q2); the estimates below assume ~25 focused hours/week. Fixed calendar dates that don't move: Golden Gate permission cut-off **4 Dec 2026**; sealed prospective window **15 Dec 2026 – 14 Jan 2027** under design P.

**Rules.**
- A milestone that runs > 25 % late loses the next item on the cut order.
- Nothing outside the MSV starts before the MSV is published, except M0.5.
- Every model, vendor and architecture choice goes into DECISIONS.md with its measurement.
- No paid call to a vendor before the owner's go-ahead for that vendor.
- Small commits; tests alongside code; CI on every push; secrets in environment variables only.

**Cut order (first to go):**
1. Unverified-place fallback.
2. LLM fallback.
3. Grand Canyon viewpoint.
4. Scrub-view polish.
5. Tier 2.
6. Post-MSV physics backlog, lowest measured gain first.

**Never cut:** the capture job, the sealed evaluation, the latency harness, the data-flow inventory.

---

## M0 — PRD and plan ✅ (pending owner answers)

- [x] Machine, repo, move-predictor and Oculus inspected; Oculus ephemeris measured against Horizons.
- [x] Five sourced research notes (`docs/research/`); NPS feeds, EXIF and Open-Meteo HRRR cutoff spot-checked.
- [x] Council: four judges × two rounds; responses in `docs/council/`; PRD v0.3 with Appendix A.
- [ ] Owner answers PRD §13.

## M0.5 — Start the clock (G → G+3)

- Forecast worker:
  - HRRR byte-range extraction (Herbie) at every viewpoint's sample points, hourly.
  - One JSON per viewpoint per run, with run start, availability, fetch time, grid cells and byte ranges.
  - Live HRRR lag per forecast hour recorded, for the hindcast availability model.
- Polite capture for Rainier (`webcams-mora/mountain.jpg`), Point Reyes and Grand Canyon:
  - Poll the JSON (or the HTTP headers for Rainier); fetch the image only on change; ≤ 4 per hour.
  - Stale and move flags.
  - Private storage at ≤ 1920 px.
  - Measure Rainier's real update cadence.
- NPS archive check: depth, cadence and camera epochs per camera → choose design **H** or **P** (PRD §8.4) and commit the split.
- Owner sends (drafts provided):
  - NPS Air Resources Division and Mount Rainier NP: public-domain confirmation for all three cameras and the archive;
  - Lawrence Hall of Science and ALERTCalifornia: Golden Gate.
- Check whether `~/Documents` is iCloud-synced; choose fixture and frame locations.

**Evidence:**
- [ ] Worker and capture logs: ≥ 72 h uptime, runs and frames per viewpoint, exclusions.
- [ ] Archive report and the H/P decision in DECISIONS.md.
- [ ] Emails sent (owner).

## M1 — Speech, parser, Tier 0, harness (G+3 → G+17)

**1a. Platform spike** (day 1, throwaway branch). Prove all of these together, and stop and report if any fails:
- hotkey press and release, including releasing the modifier first;
- a non-activating panel over another app that keeps focus;
- panel revealed on key-down, with wake-from-suspension timed;
- cpal capture opened on key-down;
- the Swift static library (C ABI) returning `DictationTranscriber` partials;
- network silence during `DictationTranscriber` (network monitor);
- a ScreenCaptureKit window-only capture with an in-canvas pixel tag;
- `CGEventPost` hotkey injection;
- host-clock agreement between ScreenCaptureKit timestamps and `Instant`;
- whether Screen Recording re-consent prompts appear.

**1b. Core.**
- Rust workspace with oculus-core pinned.
- Query lifecycle with query_gen and cancellation; tracing (no transcript text in traces).
- `SpeechEngine` trait:
  - A1 `DictationTranscriber`;
  - D Deepgram Nova-3, only after owner go-ahead: `mip_opt_out` in one function with tests; token minted in parallel with mic open, or no cloud primary.
- Parser fast path, ≥ 150 table-driven tests and property tests.
- Overlay: states, captions, chips, destination chip, on-device-only setting, Escape while visible, webview paint gate.
- Tier 0: library index, Sun-position match, background `decode()`.
- Stale-paint stress test (≥ 10k cycles).

**1c. Harness.**
- Recording tool through the live path (REC indicator that switches itself off).
- ~130 scripted utterances (30 dev / 100 test) plus ≥ 30 real-use utterances.
- Live-callback sample substitution.
- Per-pass cache namespaces.
- Warm, idle, post-wake and post-launch states.
- Overnight idle runs under `caffeinate -i` from a frozen build: ≥ 100 idle trials for the leading engine, ≥ 60 for the other.
- Cluster-bootstrap report.

**1d. README skeleton** with the headline sentence and blanks.

**Evidence:**
- [ ] Spike notes and numbers in DECISIONS.md.
- [ ] Parser suite green; case count by category.
- [ ] Replay report per engine and state: p50/p95 per stage, Tier 0 hit rate, wrong-paint rate (Wilson bound), partial lag, WER, place accuracy, share of utterances still in speech at key-up.
- [ ] STT decision (and the Parakeet gate, evaluated idle) in DECISIONS.md.
- [ ] Stress test: 0 stale paints.
- [ ] Screen recording of the overlay over another app keeping focus.

## M1.5 — Walking skeleton (by G+21 at the latest)

- Renderer v0, headless: Hosek–Wilkie sky, cloud slabs with base intersected along view rays, ceiling slab, flat fog, camera model v0.
- Extractor v0: per-landmark edge ratio vs clear-day reference.
- P1 scored on **development data only** vs persistence, climatology and S_rule.

**Evidence:**
- [ ] Dev-only P1 table, with nothing held out touched.
- [ ] Cloud Monte Carlo test output.

## M2 — Tier 1 for Rainier and Point Reyes (G+21 → G+38)

- Ephemeris validation: Horizons vectors with refraction for each viewpoint; sunrise and sunset vs NOAA to ±1 min.
- Plates:
  - pose solved from landmarks (RMS px);
  - DEM ray-cast depth plus scale-aligned near field (licence-checked model);
  - world height; sky and lights masks;
  - dehazed overcast plate and night plate;
  - DEM horizon maps;
  - model-free camera response (Kim & Pollefeys) fitted on development frames across weather.
- Renderer in the overlay and in the scrub view (a bare slider).
- Golden images in CI (SwiftShader); WKWebView tolerance documented.
- Datum of every height field pinned in DECISIONS.md.
- Physics backlog items admitted one at a time, only with a ≥ 0.02 dev-check PSS gain or an obscuration-height gain.
- S_mos fitted; analog distance and "no close analog" stratum defined; τ and the partial band fixed; MDE computed → pre-registration frozen and committed before the held-out period starts (design P: before 15 Dec).
- P2 landmark, distance, height and field mapping named.

**Evidence:**
- [ ] Ephemeris report (≤ 0.5° worst case).
- [ ] Tier 1 p95, idle and warm, from the harness, including measured pre-render contention.
- [ ] Golden-image suite green.
- [ ] Dev-split evidence for each admitted feature.
- [ ] Pre-registration commit hash recorded.

## M3 — Tier 2, then the fallbacks (after the MSV core; G+38 → G+48)

1. **Kill test** (≤ $1, after owner go-ahead for fal): 20 Tier 1 frames through Z-Image-Turbo img2img at strength 0.15–0.35; check pass rate; blind preference judged by someone else. Stop here if it fails.
2. If it passes:
   - GPU composite (sky locked, T(x)-weighted detail, ≤ 15 ms) with phase-correlation registration;
   - checks, with thresholds calibrated on a separate set;
   - false-accept suite (≥ 200 weather-altered refinements, full pipeline);
   - at most one request in flight; cancel microbenchmark (50 calls); proxy-vs-token gate (≤ 100 ms p95);
   - spend guard;
   - Runware as the alternate host;
   - FLUX.2 klein illustration (~20 calls on fal).
3. LLM fallback (Foundation Models), then the unverified fallback, unless the owner drops them.

**Evidence:**
- [ ] Kill-test result.
- [ ] Tier 2 delivered within 3 s over all queries.
- [ ] False-accept bound.
- [ ] Preference result.
- [ ] Spend ledger.
- [ ] Decision in DECISIONS.md.

## M4 — Sealed scoring and dashboard (held-out period + ~5 days)

- Extractor validation: ~150 real frames and ~50 renders per metric; 50 re-labels a week apart; bars from PRD §8.3. Held-out frames excluded from the labelling store.
- Score P1, R1–R3, P2 and the secondaries (Holm), once.
- Uptime and exclusions; iced-lens sensitivity check.
- Dashboard: private until NPS confirms; `make score` rebuilds it from frozen inputs.

**Evidence:**
- [ ] The P1 result with CI, n and base rate, whatever it shows.
- [ ] R1–R3 results.
- [ ] Plain-language fog result.
- [ ] Uptime and exclusions.

## M5 — README, demo, numbers (ships on schedule regardless of P1)

- README in move-predictor's shape, with the headline filled in.
- 60-second demo:
  - 0–10 s: ask "Rainier tomorrow at four" over an editor; frame plus trace bar;
  - 10–25 s: scrub through the evening;
  - 25–45 s: yesterday's 24 h forecast vs what the camera saw vs persistence;
  - 45–60 s: skill vs lead, with CIs.
- `THIRD_PARTY_NOTICES` generated from the licence ledger.
- Only content cleared for publication.

**Evidence:**
- [ ] Every README number traceable to a file and a command.
- [ ] `make score` works from a fresh clone.
