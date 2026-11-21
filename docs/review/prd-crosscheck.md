# Cross-check: Lookout PRD (18 Nov) vs Snow Globe PRD v0.3

Compared on 19 Nov 2026 before writing the synthesis ([PRD.md](../../PRD.md) v0.4).

> **Rename note, 21 Nov 2026:** the product is now Snow Globe (D-080). Here "Lookout" names the owner's draft of 18 Nov, and "Snow Globe PRD v0.3" the Milestone 0 draft; neither label changes.

**The two drafts:**
- **Lookout** is the owner's 34-page draft, supplied as a PDF.
- **v0.3** is the Milestone 0 draft at commit `d0ba83d`, which went through two council rounds.

**How each claim was checked:**
- against the sourced research notes in [docs/research/](../research/);
- against the Oculus repository on this machine;
- with four new lookups, listed under Sources.

**Verdicts:**
- **L** means Lookout is stronger and was adopted.
- **S** means v0.3 is stronger and was kept.
- **M** means the two were merged.
- **✗** means the claim is wrong or unsupported, and is corrected in the synthesis.

---

## 1. Errors and unsupported claims

### In Lookout

| # | Lookout says | What the evidence shows | Synthesis |
|---|---|---|---|
| L1 | "HRRR ends on 30 November 2026"; M0 moves to the replacement model (§8.3, §18.1). | Open-Meteo stops serving HRRR on that date. NOAA does not: HRRR is still publishing to AWS, and NOAA keeps it until a later RRFS upgrade ([forecast.md](../research/forecast.md) finding 1). Through Open-Meteo, the replacement (RRFS) updates only every 6 h. RRFS off-hour cycles have no low/mid/high cloud cover. | ✗ HRRR is read directly from NOAA's AWS bucket. Moving to RRFS is a labelled break, if NOAA retires HRRR. |
| L2 | Backtests use Open-Meteo Previous Runs (1–7 day leads, from Jan 2024) and Single Runs (from 2 Apr 2026). | Previous Runs offers only whole-day offsets, and `visibility_previous_day1` came back **null** for HRRR, NBM and GFS. That leaves no low-visibility backtest and no 1 h or 6 h leads. Single Runs starts 2 Apr 2026, lags by hours and may skip cycles. Open-Meteo responses carry **no run time**. | ✗ The backtest uses HRRR's AWS archive (since 2014; HRRRv4 since Dec 2020), which has exact run times, ceiling and cloud base. |
| L3 | Fog and haze come from Koschmieder with β = 3.912 / V, where V is the forecast visibility (pass 6). | HRRR VIS is a near-surface diagnostic. It sees cloud water only within ~100 m of the ground, so a deck at 300 m reads as clear. It also says nothing about aerosol haze over a 20–100 km sight line. Treating it as path extinction will mis-render distant terrain. | ✗ Clear-air haze is calibrated per viewpoint from clear-day frames. Fog and cloud come from cloud base, ceiling and layer slabs intersected along view rays. VIS applies only to the near-surface segment of the ray. |
| L4 | Cloud coverage is "calibrated per viewpoint so the rendered sky fraction equals the forecast cover" (pass 3), and a test requires agreement within 5 points (§9.3). | Forecast cover is an area fraction over a 3 km column. A camera sees cloud along slant rays that cross many columns at layer height (a 2° ray meets a 500 m layer about 14 km out). Forcing equality hides exactly the geometry the renderer exists to model. It also makes "cloud-fraction skill" a test of the calibration. | ✗ Layers are slabs, sampled where each ray meets them. The 5-point test checks the renderer against the **view-ray target fraction**, not raw cover. |
| L5 | The overlay "never takes focus", yet plain Left/Right, Tab, L, C and Esc control it (§5.4). | A non-activating panel receives no keystrokes. Registering plain arrows or Tab as global shortcuts would take them from the user's editor. | ✗ Esc is global only while the overlay is visible. The other keys work once the panel is clicked, which makes it key without activating the app, the way Spotlight-style panels work. AM/PM moves from Tab to M so Tab keeps VoiceOver focus order. |
| L6 | Golden Gate is the demo ask (TL;DR, §3.3), but with no camera it "stays a labeled demo view" (§18.1), while unverified places are deferred to Later (§4.2). | With no camera there is no plate and no truth, and unverified places are out of v1. So the headline utterance would hit **No match**. Separately, the Crissy Field cameras were "temporarily unavailable" on 18 Nov ([cameras.md](../research/cameras.md)). | ✗ The demo utterance is "Rainier tomorrow at four" unless Golden Gate permission arrives by 4 Dec. "Golden Gate at 9" stays as the product illustration. |
| L7 | Hosting is "FastAPI on AWS with Terraform"; Oculus's Terraform is the starting point, "sized for the free tier" (§6.4, §11.3, §17). | Oculus's Terraform is an ALB + ECS Fargate (2 vCPU / 4 GB) + ElastiCache stack. Oculus's own PRD §13.6 estimates **~$60/month**, which would use up the $25 development cap in under two weeks. | M AWS stays, but serverless: Lambda + EventBridge Scheduler + S3 + budget alarm. Oculus's `bootstrap/`, `storage.tf`, `cdn.tf` and `monitoring.tf` are reused; its ECS, ALB, Redis and NAT are not. |
| L8 | Milestone gates are pass/fail targets: "Tier 0 p95 at 300 ms", "90% pass the guardrails" (§16). | The brief says: "If a target is missed, give me the measured value and the bottleneck. Don't tune the benchmark." A gate that requires hitting a target either stalls work or pushes toward tuning. A pass-rate target with no false-accept bound is met fastest by loosening the checks. | ✗ A gate passes when its evidence exists (measured value + bottleneck). A miss triggers the cut-order rule. Guardrail thresholds are frozen on a calibration set and bounded by a false-accept test before the pass rate is reported. |
| L9 | Tier 0 is measured on "Replay, warm"; paint times come from in-app timestamps; the replay set has 30 utterances (§10.3, §14). | Hold-to-talk is mostly used after idle, so warm flatters. `requestAnimationFrame` fires 1–3 vsyncs before compositing. A p95 over 30 utterances is roughly the second-worst value. | S The headline is idle (≥ 30 min). Paint comes from ScreenCaptureKit with an in-surface tag. The replay set is ~130 utterances, plus ≥ 100 idle trials. Warm and cold are reported beside the headline. |
| L10 | The latency plan shows the final transcript at ~150 ms (§10 chart). | The best independently measured hosted p95 is 298 ms. On-device SpeechAnalyzer reports disagree 5× (125 ms vs 602–702 ms). | ✗ The chain allocates ≤ 240 ms, and the PRD says plainly that no engine has shown this on this machine. |
| L11 | "10,000 live pairs in 14 days" (1,152 predictions a day). | Predictions within a day at one camera are highly correlated. The independent unit is the day-viewpoint: 14 days × 8 viewpoints ≈ 112 blocks. Lookout's block bootstrap is right; the headline count overstates the evidence. | M Pair counts are reported together with the effective n and the minimum detectable effect. |
| L12 | Two primary metrics: cloud-fraction error at 24 h and low-visibility CSI at 24 h (§13.5). | Two primaries need a multiplicity correction. CSI ignores correct negatives and depends on the base rate, so it can't fairly compare against persistence across seasons. | M One primary, P1 (summit visible, ΔPSS vs persistence). Lookout's two become key secondaries, Holm-corrected, with POD/FAR/CSI still reported. |
| L13 | Persistence is "the real frame from 24 hours before the valid time". | At short leads the latest frame at issue time is a stronger baseline. | S The baseline is the stronger of (a) the latest frame at issue time and (b) the frame at valid − 24 h. |
| L14 | Pose is checked by predicting the Sun's pixel on archive frames where the Sun is visible (§8.5). | Rainier's camera looks north toward the summit, and Point Reyes looks NNE, so the Sun is never in frame in winter. | M Primary check: landmark reprojection ≤ 1°. Independent check: the time the summit loses direct sun at sunset (DEM + ephemeris), converted to degrees. The Sun-in-frame check runs where possible (Grand Canyon, summer sunsets). |
| L15 | Noise seeds "derive from the viewpoint and the valid hour" (§9.3). | Clouds would jump at every hour boundary while scrubbing at 60 fps. | ✗ Seed = hash(viewpoint, forecast run). Noise drifts with the forecast wind across valid time, so scrubbing is continuous and the output stays deterministic. |
| L16 | Speech runs in a separate Swift helper process; whether it can do so is an open question (§6.1, §20). | Foundation Models are rate-limited outside a UI process, `swift-rs` has no async support, and a helper adds IPC and permission attribution. | S An in-process Swift static library with a C ABI. The open question is closed. |
| L17 | The SpeechAnalyzer forum thread reports "a hotkey crash on macOS 26". | The crash (forum 819525) is in the `NSEvent` global monitor, which only the Fn-key path would need. The Carbon `RegisterEventHotKey` path used by the Tauri plugin is unaffected. | M The risk is attached to the Fn trigger, which is already in Later. |
| L18 | Storage: "Ten cameras at 200 KB a frame ≈ 50 MB a day." | Measured on 19 Nov: Rainier 1080p is 577 KB and Point Reyes 4K is 1.86 MB. | ✗ Hourly frames are stored at ≤ 1920 px (~0.5 MB) and 15-minute frames as 640 px thumbnails: ~17 MB per camera per day, or ~135 MB a day for 8 cameras (estimate). |
| L19 | oculus-core needs the Moon's illuminated fraction added (§8.4). | It already exists, measured at 0.01 % error. | ✗ Only refraction and the camera-frame transform are new. |
| L20 | "Tier 2 hides some of this" for missing shadows and the albedo proxy (§9.2). | When Tier 2 is rejected, those errors are what remains on screen. | S DEM horizon shadows are in Tier 1 itself. |
| L21 | Image area is 16:10 (§5.1). | Every candidate camera is 16:9. Cropping would cut landmarks the caption refers to. | ✗ The image area uses the camera's aspect ratio. |
| L22 | Overlay tiers cross-fade in 120 ms (§5.5). | When paint is measured at ≥ 50 % opacity, a 120 ms fade adds ~60 ms to Tier 1 and Tier 2. | M Tier 0 appears without a fade. Tier 1 and Tier 2 cross-fade, and the fade is part of the measured paint time. |
| L23 | Cloud fraction is measured with a red-to-blue ratio threshold in daylight (§13.3). | Twilight skies are red and near-horizon haze is white. On landscape cameras both register as cloud. | M Measured against an illumination-conditioned clear-sky reference, restricted to Sun ≥ 10°, validated on ≥ 150 labelled frames. |
| L24 | Sigg et al., 2022 is the closest prior work (§2). | **Verified:** *AIES* 2022, arXiv 2203.15601. Three Swiss cameras, COSMO-1 forecasts, raters near chance, conditions matched in ≥ 89 % of examined cases. Images were 64×128 px (ECMWF workshop slides), and agreement was scored against the forecast's conditions, not the frame the camera later recorded. | L Kept; the details are added. |

### In v0.3

| # | v0.3 says | What Lookout shows | Synthesis |
|---|---|---|---|
| S1 | Hosek–Wilkie for Sun ≥ 0°, plus a separate twilight model (§6.5). | "Fitted daytime sky models are weakest at twilight, which is when people ask." Two models stitched together at 0° put the seam at sunset. | L Hillaire 2020 LUT atmosphere at every Sun angle, ported from `sebh/UnrealEngineSkyAtmosphere` (MIT). Babylon.js (Apache-2.0, GLSL) is the alternative. Hosek–Wilkie is dropped. |
| S2 | No list of overlay states, no caption forms for Tier 0 and live, no controls, no motion or accessibility rules. | §5 covers all of them, plus copying an image with its label burned in. | L Adopted, with the focus fix (L5). |
| S3 | Open questions were a numbered list at the end (§13). | A "Decisions to confirm" table up front, with consequences and a "Your call" column. | L Adopted. v0.3's 11 questions are merged into it. |
| S4 | Three viewpoints. | Eight. Capture is cheap; building is not. | M Capture up to 8 NPS cameras from M0.5. The MSV claims Rainier and Point Reyes. v1 grows toward 8 after the MSV is published. |
| S5 | The prediction log was "a small SQL DB". | Parquet in object storage, queried with DuckDB. | L Adopted: append-only, with no server to run. |
| S6 | No API routes or jobs table. | §11.1–11.2. | L Adopted and adjusted: the app reads forecast files directly from storage. |
| S7 | Leads of 1, 6 and 24 h. | Leads of 1, 3, 6, 12, 24 and 48 h. | L Adopted where the model run exists: 24 h and 48 h come only from the 00/06/12/18Z HRRR runs. |
| S8 | No "graded asks" hook, no ungrounded-generation baseline, no per-camera reporting rule. | All three. | L Adopted. The ungrounded baseline is capped at ≤ $1 and needs vendor approval. |
| S9 | No frame quality gate for lens clarity or colour mode. | Four gates. | M Merged with v0.3's staleness and camera-move checks. |
| S10 | Bare-hour rules only. | A time-rule table: day + bare hour (7–11 means AM, 12 noon, 1–6 PM), part-of-day defaults, follow-ups that reuse the last place, cross-zone tests. | L Adopted. v0.3's DST handling and solar-event instants are kept. |
| S11 | Ephemeris budget 0.5°. | Sun ≤ 0.05°, Moon ≤ 0.3°, plus 1,000 random samples against Skyfield. | L Tighter targets adopted; the measured 0.19′ (0.003°) already passes them. Both tests kept. |
| S12 | No repository layout, no PRD_PROGRESS.md, no "write the gate's test first". | All three. | L Adopted. |
| S13 | Depth from DEM ray-cast plus a near-field model. | Monocular depth with landmark scale. | S DEM for terrain at 8–100 km; monocular depth only for the near field, aligned to the DEM. |
| S14 | Night lights from a "night plate". | Lights mask = median night − median day, with an on/off curve fitted to Sun altitude. | M Same idea; Lookout's procedure is clearer. |
| S15 | Unverified places were in v1, last in the cut order. | Moved to Later. | L Agrees with v0.3's recommendation (§13 Q9) to drop them. |
| S16 | No prompt rule for Tier 2. | The prompt is built from parameters, so the user's words never reach the image model. | L Adopted. |
| S17 | The Tier 2 survival rule had no explicit pass-rate metric. | A guardrail pass rate is reported. | M Reported, after false-accept calibration (L8). |

## 2. Where the two drafts agree

- Tauri 2 shell, one WebGL2 renderer shared by the app and headless evaluation.
- oculus-core for the ephemeris.
- NPS cameras for evaluation.
- Tier 0 as an analog baseline as well as a screen.
- Block bootstrap over days.
- Primary metrics fixed before the first backtest.
- Captions on every frame.
- Keys only on the server.
- $25 cap, and ask before each paid call.
- Guardrails with fallback to Tier 1.
- Fog reported as measured, even if it shows no skill.

## Sources (new lookups, 19 Nov 2026)

- Sigg et al. 2022: [arXiv 2203.15601](https://arxiv.org/abs/2203.15601); [ECMWF ML workshop slides](https://events.ecmwf.int/event/294/contributions/3062/attachments/1761/3184/ML-WS_Sigg.pdf) (64×128 px).
- GitHub Actions disables scheduled workflows after 60 days without activity in a public repository: [docs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
- EventBridge Scheduler includes 14 M free invocations a month: [AWS pricing](https://aws.amazon.com/eventbridge/pricing). The Lambda free allowance (1 M requests, 400k GB-s) is from secondary sources ([wiz.io](https://wiz.io/academy/cloud-cost/aws-lambda-cost-breakdown)) and is checked on AWS's page before deployment.
- Oculus hosting cost: `~/Documents/Oculus/oculus/PRD.md` §13.6 ("~$60/mo") and `infra/terraform/{ecs,redis,network}.tf`.
- Everything else: [docs/research/](../research/), dated 18–19 Nov 2026.
