# Council round 1 — author's responses

Reviewed document: PRD.md v0.2. Judges: latency engineer (LE), graphics/ML researcher (GR), skeptical hiring manager (HM), privacy and licensing reviewer (PL). This file lists what changes in v0.3, what is rejected and why, and answers to each judge's follow-up questions. Items marked **[owner]** are decisions only the owner can make; they go to PRD §13.

---

## A. Cross-council changes accepted for v0.3

1. **Minimum Shippable Version (MSV)** (HM B2): push-to-talk → Tier 0 and Tier 1 on two viewpoints, traced latency, a sealed and pre-registered skill result, README. Tier 2, the unverified fallback and the LLM fallback are owner requirements and stay in the plan, but are sequenced after the MSV core and kept minimal. A ranked cut order is added.
2. **Walking skeleton** (HM B1): by the end of M2's first week, renderer v0 (sky, uniform cloud, flat fog) runs headless and is scored against persistence on dev data; each later renderer feature ships only with a measured improvement on dev data. M2 gets a calendar end date [owner: hours/week].
3. **Capture and forecast worker start immediately after go-ahead** (HM protect-at-all-costs), all three viewpoints, before the rest of M1.
4. **Viewpoints** (HM B4): Mount Rainier is the hero ("Is the mountain out tomorrow at 4?"); Point Reyes stays as the fog viewpoint (captured from day one, scored on its own season); Grand Canyon is captured but built third, after the MSV. Golden Gate gets a permission deadline [owner].
5. **Evaluation redesign** (GR B2, B5; HM B3):
   - **Hindcast first.** HRRR (2014–), GOES-18 and NBM are archived on AWS; if the NPS archive is deep enough (verified as the first M1 task), past frames are scored: development on earlier periods, held-out on a later season, including a summer fog season for Point Reyes. A **sealed prospective window** (proposed 15 Dec 2026 – 14 Jan 2027, ≥ 28 days) is confirmation.
   - **Pre-registered primary endpoints** (one or two per viewpoint), **paired error differences** as the primary statistic, block bootstrap by day, **Holm** correction on all secondary cells. The skill-score ratio is secondary, computed over whole periods only.
   - **New first-class baselines:** climatology; **forecast-scalar** (HRRR TCDC/VIS/ceiling mapped directly to the observable, no rendering); **forecast-class analog** (Tier 0 matched on Sun position *and* forecast class).
   - **The renderer's claim is restated.** The forecast supplies skill; the renderer must be *non-inferior to the forecast-scalar baseline* (it renders the forecast faithfully). The README says so plainly, including if the analog wins.
   - **One extractor** is applied to predictions, baselines and truth; it is validated on labelled real frames *and* ~50 labelled renders. Results are stratified by analog distance.
   - **Metrics:** primary = event endpoints (summit visible; landmark obscured) and **obscuration height** (m ASL below/above which terrain is hidden). Secondary = the owner's list (cloud fraction, fog contingency, brightness, colour, embedding similarity). Embedding cosine is demoted to a diagnostic; **retrieval rank** of the true frame among same-Sun candidates replaces it as the image-similarity metric.
   - **Oracle ladder** replaces the single oracle: forecast → HRRR f00 at valid time → independent observations (GOES, on-site met) → camera-extracted inputs (labelled circular).
   - Double-label 50 frames for agreement; fog F1 only reported with ≥ 30 positives; day-only scoring in v1.
6. **Clouds** (GR B1): the "packing from perspective" claim is withdrawn (it is false for a thin layer). Layers get vertical extent; rays intersect each layer with Earth curvature and read the forecast at the intersection; threshold before filtering; overlap rule checked against TCDC; a Monte Carlo test (≥ 1,000 seeds × f ∈ [0.1, 0.9]) reports rendered fraction by elevation band; several seeds are averaged in scoring.
7. **Fog truth** (GR B3): primary truth = **binary per-landmark visibility** via an edge-energy-vs-clear-day comparator (VEIA-style), with illumination-conditioned clear-day references; continuous visibility is secondary, scored as **censored ordinal bins** bounded by landmark distances; fog-top height via world height. Camera response is fitted render-independently on clear frames (sky-gradient calibration), few parameters, published with it on and off, no valid-time statistics.
8. **Renderer physics fixes** (GR other): dehaze the overcast plate using depth before relighting; no double-counted sky occlusion; DEM horizon maps for shadows (oculus-core has DEM horizon code to evaluate); snow state from HRRR for Rainier; rain as extinction and wet-surface darkening, no streaks; Hosek–Wilkie only for Sun ≥ 0°, twilight from a separate model and scored separately; pinned datum (AGL vs MSL) for every height field; multiple-scattering airlight approximation in thick fog; Grand Canyon haze metric dropped (aerosol, not in HRRR VIS); "camera inside the marine layer" reported as its own contingency.
9. **Tier 2 by construction** (GR B4): Tier 1's sky is composited back through the sky mask; only high-frequency ground detail from the refiner is transferred, weighted by Tier 1 transmittance T(x), so hazed ridges cannot be dehazed. The checks are validated for **false accepts** on ≥ 200 deliberately weather-altered refinements (0 accepts → FA ≤ 1.5 % at 95 %); acceptance is reported by weather class; the shown frame is scored against a non-inferiority margin. Shortlist narrows to **Z-Image-Turbo img2img** (fal and Runware); FLUX.2 klein is kept only as a ~20-call control to test the drift claim (< $0.50).
10. **Latency measurement** (LE B1–B5):
    - t0 = OS key-up event timestamp (Carbon `GetEventTime` or a listen-only event tap in measured runs); handler gap reported.
    - Paint = first WindowServer-composited frame showing the tier at ≥ 50 % opacity, captured per query by ScreenCaptureKit with a pixel tag (query_gen, tier); rAF and IPC stamps are diagnostics with recorded uncertainty. Crossfade 100 ms; "settled" also reported.
    - Per-pass cache namespaces in measured runs; hit rate reported against a stated usage model.
    - Named states: warm (< 60 s since last query), **idle (≥ 30 min, the headline)**, post-wake, post-launch; the harness reproduces the gaps; per-trace idle duration, cache/connection/model state, thermal and power state, network interface.
    - Utterances: ~130 distinct (30 dev, 100 test) [owner: recording time]; cluster bootstrap by utterance; runs over ≥ 3 days and several times of day, engines interleaved; fixtures recorded through the live open-on-key-down path.
    - Correctness contract: the webview checks (query_gen, scene_hash, tier) immediately before compositing; Tier 0 latency = key-up → paint of the frame matching the *final* parse; wrong paints never count as hits and are reported as visible-wrong-ms; written partial-stability rule; partial lag per engine; a ≥ 10k-cycle randomized key-mash stress test with injected delays asserting zero stale paints.
    - One budget chain per tier from key-up that sums to the target; the Tier 0 allocation states openly that STT final is the risk (research: 298 ms hosted p95, 600–700 ms in one SpeechAnalyzer report) and that an LLM-fallback rate above ~5 % fails Tier 0 by construction.
    - Tier 2: ≤ 1 request in flight with a replace policy; provider-reported queue/inference times logged; headline is "Tier 2 delivered within 3 s" over *all* queries (rejections, errors, timeouts are misses).
    - **New headline metric:** p95 key-up → first *forecast-driven* frame (Tier 1), target ≤ 400 ms, alongside the owner's three tier targets.
    - Cut from v1: speculative Tier 2 and key-down warm-up beyond a TLS pre-connect. Tier 1 may still pre-render (not paint) from a stable partial; it costs nothing.
    - GPU time via `fenceSync`; shader variants pre-warmed after launch and after context loss; GPU-resident plates ≤ 1920 px wide; Tier 0 full image decoded with `decode()` before swap; no network call on the Tier 1 path for curated viewpoints; LIVE fetch in the background.
11. **STT scope** (HM, LE): engines A1 (`DictationTranscriber`) and D (Deepgram Nova-3) first. P (Parakeet) is added only if A1's measured release → final p95 exceeds 250 ms. A2 (SpeechTranscriber), Flux and the re-decode race are dropped unless measurement calls for them. Trailing pad = **synthetic silence** (no capture after release, honouring "audio only while the key is held").
12. **Privacy and licensing** (PL B1–B5):
    - A **data-flow inventory** table in §9 with owner, retention and disclosure for every item.
    - Traces hold IDs, lengths and parse fields, never transcript text (except in replay runs); local only; 30-day rolling retention.
    - Backend log field list (random install_id, viewpoint_id, valid_time, tier timings, check results; no transcript, no IP stored by our code; the host's own logs disclosed) with 90-day retention; per-install auth on the proxy.
    - The destination chip covers *any* text or audio leaving the machine, and changes *before* any fallback upload; an on-device-only setting; `mip_opt_out=true` set in one function with tests; short-lived STT credentials minted by the backend (verify Deepgram/Soniox token APIs in M1); no pre-roll buffer.
    - Verify `DictationTranscriber` makes no network calls before labelling it on-device.
    - Frames, plates and thumbnails are never committed to git; private storage at ≤ 1920 px; raw-frame retention ends 12 months after M5 except frames used for plates and published examples; published images ≤ 1280 px with people and vehicles blurred and reviewed.
    - **Public-domain basis** is written per camera; the contractor EXIF tag on Point Reyes frames means **written confirmation is requested from NPS Air Resources Division** before any frame is published; until then the dashboard stays private.
    - Takedown section: channel, owner, response time, every copy.
    - Licence ledger + generated `THIRD_PARTY_NOTICES`: Hosek–Wilkie data tables are **BSD-3** (notice kept); oculus-core MIT inside the WASM bundle; Parakeet weights CC-BY-4.0; NOAA credit; Open-Meteo link next to any display plus "modified"; Open-Meteo free tier non-commercial recorded; no implied NPS/NOAA endorsement ("derived from NPS webcam imagery; not an NPS product").
    - Corrected wording: "Soniox stores no audio or transcripts; diagnostic metadata may be retained temporarily".
    - Exported or published images carry the label **burned into the pixels** plus IPTC `trainedAlgorithmicMedia` (or C2PA) metadata for refined and unverified frames; the unverified-place provider is named before it ships.
    - Depth, embedding and DEM sources are named with licences (no CC-BY-NC weights such as Depth Anything V2 Base/Large).
    - Capture: poll the NPS JSON and fetch the 4K frame only when `imagedate` changes; separate rate limit for archive backfill; contact URL in the User-Agent; Rainier's interval measured; installed app copies obey the same rules for LIVE.
13. **Housekeeping** (HM other): PLAN/PRD contradictions fixed (forecast source, cron accuracy, capture interval); DECISIONS.md citations fixed; CI on every push including leakage tests (baselines only see frames available at issue time) and metric unit tests; `make score` reproduces the dashboard from frozen inputs; hosted dashboard for reviewers (after NPS confirmation); the 60-second demo script adopted (yesterday's forecast vs what the camera saw).

## B. Rejected or modified, with reasons

- **Cut the unverified-place fallback and both LLM fallbacks (HM).** Rejected as cuts: both are in the owner's brief. Modified: they are sequenced after the MSV core, kept to one candidate each (LLM: Foundation Models in-process, $0, no text leaves the machine; Haiku only if FM is unavailable or fails accuracy), and the owner is asked whether to drop them.
- **Make Tier 0 only an eval baseline (HM B5).** Rejected as a cut: Tier 0 is the owner's spec. Modified: the new "first forecast-driven frame" headline metric is added, and the owner is asked whether Tier 0 should be skipped on screen when Tier 1 is ready within ~100 ms of it.
- **Drop FLUX.2 klein entirely (GR).** Modified: kept as a ~20-call control (< $0.50) so the README can show, rather than assert, why reference editors fail the checks. Not a candidate.
- **Full censored-likelihood β fit as primary fog truth (GR B3).** Modified: binary per-landmark visibility is primary in v1; the continuous fit is secondary.
- **Drop the third viewpoint (HM).** Modified: all three are captured from day one (calendar time can't be bought back), but only two are built before the MSV.
- **Cut NBM (HM).** Accepted for v1 build; reversible later because NBM is archived on AWS and can be hindcast.

## C. Answers to follow-up questions

### Latency engineer
1. **t0:** the OS key-up event timestamp. If `tauri-plugin-global-shortcut` doesn't expose the Carbon event time, measured runs add a listen-only event tap (Input Monitoring) that records it; the p95 gap to the handler's `Instant::now()` is reported. Not yet measured.
2. **Crossfade:** 100 ms. "Paint" = first composited frame where the new tier is at ≥ 50 % opacity (the pixel tag is drawn at that point); "settled" = crossfade end, also reported.
3. **Stability rule:** a partial is stable when its parse yields a complete (place, time) above the confidence threshold and the same parse has held across ≥ 2 consecutive partials spanning ≥ 150 ms of audio. Tier 0 **never paints before key-up**. At key-up, if the final hasn't arrived and the latest partial is stable, Tier 0 paints from it; if the final differs, the replacement is counted as visible-wrong-ms.
4. **Trailing pad:** synthetic silence pushed after release (no capture after key-up), length swept 0–300 ms on the dev half; its compute cost is inside the budget.
5. **Cold/idle:** idle = ≥ 30 min since the last query with the panel hidden. It exercises whatever the system does naturally in that time: webview suspension, model eviction (state recorded, not forced), connection idle timeouts, provider scale-to-zero. Post-launch and post-wake (scheduled sleep/wake) are separate states.
6. **Sample:** proposed 130 distinct utterances (30 dev, 100 test) [owner]; bootstrap unit = utterance; runs over ≥ 3 days, morning/afternoon/evening, engines interleaved.
7. **Superseded Tier 2:** no speculation in v1. On a new query: abort the HTTP request *and* call the provider cancel API; the slot is verified free by the next request's provider-reported queue time; wasted calls and dollars are logged.
8. **Proxy:** host undecided [owner]. Preference is the direct short-lived-token path if the provider supports it, otherwise a host without scale-to-zero cold starts. The added p95 is measured both ways before choosing.

### Graphics/ML researcher
1. **Camera elevations / in-cloud fraction:** not known yet; surveyed in M2 from NPS location data and the DEM; the in-cloud fraction of dev frames is measured and reported.
2. **Predicted value:** the extractor run on the *rendered* frame (the same extractor as truth). The SceneSpec scalar mapping is the forecast-scalar baseline.
3. **Landmark distances / censoring:** determined from the DEM in M2. Continuous visibility is scored as ordinal bins bounded by landmark distances, with the top bin censored at ~80 × d_max.
4. **Primary endpoints (proposed for pre-registration):** P1 Rainier "summit visible" (day frames), 24 h lead, paired proportion-correct difference vs the stronger persistence baseline. P2 Point Reyes "far headland obscured", 24 h lead, same statistic, on the hindcast summer season if the archive allows (else declared underpowered). The minimum detectable effect is computed from dev-period variance and published *before* sealing.
5. **Archive depth:** unknown; verifying it is the first M1 task, and the dev/held-out split is fixed after.
6. **"Never invents weather":** operationally, 0 false accepts on ≥ 200 labelled weather-altered refinements (FA ≤ 1.5 % at 95 %), plus by-construction compositing of sky and transmittance-weighted content.
7. **If the analog wins everywhere:** the README says so. The shader's claim becomes non-inferiority to the forecast scalar plus coverage: it renders forecast combinations and times with no close analog, and the stratification by analog distance shows where that matters. It is not retired.
8. **Camera model:** ~7 parameters (exposure response to scene luminance ×2, tone curve ×2, white balance ×2, vignette ×1; noise and JPEG fixed from metadata), fitted on clear-sky dev frames by sky-gradient calibration, render-independent, published on/off.

### Hiring manager
1. **End date and hours/week:** [owner]. The cut order (MSV first) decides what goes if M2 overruns.
2. **README headline (draft with blanks):** "On a pre-registered, sealed __-day window, Snow Globe's 24-hour visual forecast of whether Mount Rainier is visible was right __ % of the time versus __ % for persistence (difference __ points, 95 % CI __ to __), and its first forecast-driven frame appears __ ms (p95, after 30 min idle) after you release the key."
3. **No skill → M5 still ships on schedule:** yes, committed.
4. **STT engines:** SpeechAnalyzer `DictationTranscriber` and Deepgram Nova-3. A third (Parakeet) only if A1's release → final p95 > 250 ms.
5. **Golden Gate deadline:** proposed: emails sent this week; no written yes by the end of M2 → Golden Gate out of v1 [owner].
6. **Tier 2 survival:** ≥ 80 % of queries get an accepted Tier 2 within 3 s *and* a blind preference test (≥ 50 pairs) prefers Tier 2 over Tier 1 in ≥ 70 % of pairs. Otherwise it ships off by default and the README says why.
7. **move-predictor's gap:** its README reports 22–26 % top-1 against a > 36 % target. The countermeasures here: pre-registered endpoints and a sealed window, MSV first with a cut order, a README skeleton with blanks written in M1, and every target reported with its measured value.

### Privacy and licensing reviewer
1. **Traces and backend log:** owner = the project owner. Traces are local, contain no transcript text, 30-day rolling retention. The backend log holds install_id, viewpoint_id, valid_time, timings and check results; no transcript, no IP stored by our code; 90-day retention; the host's access logs disclosed.
2. **Fixtures:** stored outside any iCloud-synced folder (location checked in M1), excluded from git, deleted after M5 or on request. Sent to Deepgram only with the owner's approval, with `mip_opt_out=true` ("only for the duration necessary to process the request").
3. **Per-camera public-domain basis:** the NPS disclaimer plus written confirmation requested from NPS Air Resources Division, because of the contractor EXIF tag. Until confirmed: capture and private evaluation only; nothing published.
4. **Raw frames:** private storage at ≤ 1920 px; deleted 12 months after M5 except plate sources and published examples. Before publishing: ≤ 1280 px, automated person/vehicle blur, manual review.
5. **Takedown:** the owner, via a GitHub issue template and a contact the owner chooses [owner]; response within 7 days; deletion from storage, CDN, dashboard and the Tier 0 index (local caches purge when the index changes).
6. **UNVERIFIED provider:** named in M3 before it ships; candidate is Z-Image-Turbo text-to-image on the chosen Tier 2 host with request history off and a minimal CDN lifetime; label burned in plus IPTC `trainedAlgorithmicMedia`.
7. **Cloud STT credential:** a backend-minted token with a TTL ≤ 60 s (token API verified in M1); `mip_opt_out` is set in one function with a unit test and an integration test on the request URL.
8. **Attribution text:** NPS — "Webcam imagery courtesy National Park Service, Air Resources Division. Derived images are not NPS products." NOAA — "Forecast data: NOAA HRRR via NOAA Open Data Dissemination." Open-Meteo — "Weather data by Open-Meteo.com (CC BY 4.0), modified", linked wherever it is displayed. Hosek–Wilkie — the BSD-3 notice in `THIRD_PARTY_NOTICES` and the shipped bundle. Parakeet — "NVIDIA Parakeet TDT v2, CC BY 4.0" if used. oculus-core — the MIT notice in the WASM bundle.
