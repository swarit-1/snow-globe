# app/src: the overlay's web layer

`@snow-globe/overlay` 0.1.0 is the overlay UI that the Tauri shell's webview will load. It is framework-free TypeScript (PRD §6.1, §12), with tickets M1-07 (overlay) and M1-13 (stale-paint stress test). It holds:

- the states of PRD §5.2;
- the caption of §5.3 and the chips of §5.1;
- the keys of §5.4 and the copy, motion and accessibility rules of §5.5;
- the core-to-webview message protocol;
- the webview paint gate of §10.3;
- the webview half of the Tier 0 background decode of §9.1 (a frame cache that `prefetch` fills).

Nothing native is here: no Tauri, no hotkey, no panel. PLAN.md M1 says the app shell waits for the platform spike's results.

```sh
cd app/src
pnpm install --frozen-lockfile --prefer-offline
pnpm test               # tsc, then vitest: unit (Node) and browser (headless Chromium)
pnpm stress             # the browser key-mash run alone, JSON report on stdout (64 lanes x 160 cycles)
pnpm preview            # writes app/dist/preview.html: every state with sample captions
pnpm build              # writes app/dist/overlay.js, the ESM module the shell will load (stops if the schema is stale)
node scripts/schema.ts  # rewrites protocol.schema.json after a change to overlay/protocol.ts
```

`pnpm test` took 120 s on the M4 (22 Nov 2026, load average about 12 from concurrent work): under 2 s of unit tests, then the browser runs, the stress run being most of it. The browser tests use Playwright's installed headless shell (`playwright-core` 1.65.0). Pages are served from an intercepted `https://snow-globe.invalid` origin, so no request leaves the machine. app/dist/ is git-ignored.

## Files

| Path | What |
|---|---|
| `overlay/protocol.ts` | The messages, defined once with small combinators (`overlay/schema.ts`). They give the TypeScript types, the runtime validator and `protocol.schema.json` together. |
| `overlay/gate.ts` | `PaintGate`, rule for rule the `PaintGate` of `app/core/src/lifecycle.rs`, plus `clearScene`; also the FNV-1a scene hash. |
| `overlay/scheduler.ts` | The paint path: decode, then a frame callback, then the gate, then the draw; the Tier 0 skip rule (D-012 row 11); paint timestamps. |
| `overlay/cache.ts` | The frame cache: `prefetch` decodes frames in the background, so a Tier 0 at key-up is a swap (PRD §9.1). |
| `overlay/controller.ts` | Where messages meet the gate, the paint path and the state machine; the keys' commands; the destination acknowledgement. |
| `overlay/state.ts` | The PRD §5.2 state machine as a pure `reduce`, and the predicates the controller shares with it. |
| `overlay/caption.ts`, `overlay/chips.ts`, `overlay/format.ts` | Caption and chip text; dates, times and units through `Intl`. |
| `overlay/view.ts`, `overlay/styles.ts` | The DOM: canvas layers, Web Animations cross-fades, chips, marks, pill, status line, caption, live region, keys. |
| `overlay/tag.ts` | The in-surface pixel tag of PRD §10.4, the layout of the spike's `common/src/tag.rs`. |
| `overlay/index.ts` | `mountOverlay`, `fetchLoader`, `installGlobal`. |
| `overlay/samples.ts`, `overlay/fixtures/core-parser-outputs.json` | Sample messages. The queries are real output of the core's parser; the display names follow the viewpoint manifests (a test checks). |
| `overlay/fakes.ts` | A simulated clock, a recording surface and loaders, for the Node tests only. |
| `harness/keymash.ts` | The Rust key-mash ported line for line to drive the webview gate. |
| `harness/stress.ts`, `harness/mutants.ts` | The browser stress run and its ten deliberately broken overlays. |
| `preview/preview.ts` | The preview page (24 panels). |
| `test/*.browser.test.ts` | Playwright tests: the stress run, the DOM view, the frame cache's measured swap, and the preview. |

## The protocol

Every message carries `v: 1` and a `type`. Inbound (core → webview):

| Type | When |
|---|---|
| `query_started` | Key down. A new generation; everything older is cancelled. |
| `partial`, `audio_level` | While the key is held: the pill's text and waveform (a level, never audio). |
| `released` | Key up. |
| `parsed` | The core's query (exactly as `snow-globe-core` serialises it, with the D-040 fields), its scene hash and the display names. It may come on a stable partial, before key-up (PRD §7.2). |
| `scene_changed` | A scrub, an AM/PM flip, another place, or the Out of range offer taken (`cause: use_offer`) for the same query. Tiers start again. |
| `tier_started` | A tier is due in `eta_ms` (feeds the skip rule). |
| `tier_frame_ready` | A frame and its label. Tier 0 must carry a photo label, Tier 1 a simulated one and Tier 2 a refined one, so a simulated frame can never be labelled a photo. |
| `tier_rejected` | A tier will not come (PRD §9.7). |
| `no_match`, `out_of_range` | The parser's outcomes, as its error JSON gives them. Either withdraws a scene shown on a stable partial. |
| `cancelled` | Esc or key-up with no speech. |
| `error` | A code and `fatal`; codes only, never free text. |
| `settings`, `destination`, `network` | On-device only, units, locale, Reduce Motion, harness mode; the destination chip; offline. |
| `prefetch` | Frames the core expects to ask for soon: the webview decodes them in the background. Nothing paints from it. |

Outbound (webview → core):

- `ready`;
- `paint`, the fate of each frame with page timestamps, whether its pixels were prefetched, and on the first frame after a scrub how long the previous scene's frame had stayed up (`previous_ms`);
- `destination_shown`, per key press (`seq` and `query_gen`);
- `dismiss`, `scrub`, `flip_meridiem` and `copy`, for the keys;
- `use_offer` and `open_url`;
- `protocol_error`.

Rules the schema cannot state, which the core and the shell must keep:

- **Order.** Messages arrive in send order, on one channel.
- **Generations.** `query_gen` rises by one per key press, from 1, below 2^53.
- **Scene hashes** are 16 lower-case hex digits. A JSON number cannot carry a u64 in JavaScript, and `SceneHash` serialises as a bare number today, so the shell converts it.
- **Tiers** are the integers 0, 1 and 2. lifecycle.rs serialises `Tier` as `"tier0"`, `"tier1"` and `"tier2"`, so the shell maps them both ways (inbound tiers and the outbound `paint`).
- **Frames** are served under `snowglobe://localhost/` or Tauri's `asset://localhost/`; the schema refuses any other URL, so the webview never fetches a remote camera (PRD §15.3). A `prefetch` and the later `tier_frame_ready` must name the same URL for the swap.
- **Every tier gets an answer.** For each scene the core sends a frame or `tier_rejected` for every tier that is on, so the marks settle and a scene that can show nothing says so.
- **The destination chip comes first** (PRD §15.1). In each key press the core sends no byte to a cloud destination until `destination_shown` for that destination's `seq` and that `query_gen` arrives. The webview sends it two frame callbacks after the chip is on screen in that key press, never while idle (the panel shows nothing), and never for a destination that contradicts on-device only (that is a `protocol_error`, and the chip shows it as a warning).
- **Times.** Every time is RFC 3339 and a real instant; time zones are IANA zones.
- **Transcripts** are at most 10,000 code points (the core truncates beyond); the panel clips what it shows.

`parseInbound` enforces the semantic ones too. A refusal names the path and the rule, never the content, so no transcript can reach a log.

## The paint gate (PRD §10.3)

- **When the gate is checked.** A frame is checked when it arrives (it is not decoded if refused) and again in the animation-frame callback that draws it. That second check is `gate.tryPaint`, in the same synchronous block as the canvas draw and the caption update, so nothing can run between check and draw.
  - Measured in the stress run: 0.040 ms at p50 and 0.150 ms at p99 from the check to the end of the draw.
- **Gate rules** are lifecycle.rs's: generation, then dismissed (Esc), then scene, then tier order. Two deliberate differences:
  - `begin` ignores a generation that is not newer, so a duplicate `query_started` cannot reopen a gate Esc closed.
  - `clearScene` (no Rust counterpart yet): a No match or Out of range withdraws the scene of a stable partial, so nothing of it stays or paints later, while an answer of the same key press (the offer taken) can still open a new scene.
- **One rule for gate and panel.** Whether a scene message or an outcome takes effect is one predicate (state.ts `acceptsScene`, `acceptsOutcome`) for both, so frames never paint under a message the panel ignored.
- **Reset.** A new key press, Esc, a fatal error or a withdrawn scene aborts every decode and clears the surface at once, fades included.
- **Tier 0 skip rule** (D-012 row 11). Tier 0 is held while Tier 1 of the same scene is decoding, or announced as due within 100 ms of Tier 0 being ready; an announcement is used up when its frame arrives, so a Tier 1 that then fails releases Tier 0 at once. A held Tier 0 is skipped if Tier 1 comes in time; otherwise it paints in the last frame callback that starts before the 100 ms deadline. While holding, the scheduler takes every animation frame (no timer), so only a frame callback that itself runs late can push the draw past the window: measured max 97.95 ms in the final run, 101.2 ms in the run before it (64 lanes on one main thread). It still runs, and its times are reported.
- **A scrub's previous frame.** After a scrub or AM/PM flip, the old frame keeps its own, true caption until the new scene paints; the chips already show the new time. If that takes over 250 ms a "Previous time" badge says so; if every tier of the new scene is rejected or fails, the old frame is taken off and the panel says there is nothing to show. The replacing paint reports how long the old frame stayed (`previous_ms`, visible-wrong time).
- **Cross-fades.** Tier 1 and Tier 2 cross-fade in 120 ms with the Web Animations API, unless Reduce Motion is on. The frame at which a fade passes 50 % is reported; a fade cut short (its layer replaced or cleared first) is reported without one.
- **Pixel tag.** In harness mode the tag is drawn into each tier's own canvas, so it fades with the tier. It decodes as the new tier from the first frame at ≥ 50 % opacity, which is the PRD §10.4 paint definition.
- **Diagnostics only.** Page timestamps are diagnostics: the measured paint is the composited frame (§10.4).

## The frame cache (PRD §9.1)

`prefetch` names frames; the cache decodes them with createImageBitmap off the main path, bounded at 32 frames and 64 MiB (least recently used first; pixels on screen are freed only when off screen). A `tier_frame_ready` for a cached URL takes a lease on those pixels; any other is decoded on the spot.

Measured in headless Chromium (`test/prefetch.browser.test.ts`, final run): 40 synthetic 640 × 360 JPEGs (88.5 KiB each), half prefetched while the key was held, through the real overlay. The bytes come from memory, so the fetch from the shell's scheme is not in these times.

| | received → pixels ready | received → drawn |
|---|---|---|
| Prefetched | p50 0.020 ms, max 0.075 ms | p50 0.185 ms, max 0.395 ms |
| Decoded at key-up | p50 2.29 ms, max 3.92 ms | p50 8.24 ms, p95 19.8 ms |

The decode alone took p50 1.54 ms. "Drawn" includes the wait for the next frame.

## The stress test

**Two halves:**

1. `harness/keymash.ts` replays the Rust simulation (`app/core/tests/keymash.rs`) through the webview gate: the same SplitMix64 streams, event order and oracle.
   - On the four Rust seeds it reproduces every Rust count exactly: 40,000 cycles, 0 stale paints, and the same drops by reason.
   - It reproduces the Rust broken gates' counts too.
   - That is the evidence that the two gates decide alike (`harness/keymash.test.ts`).
2. `harness/stress.ts` runs the real overlay, DOM view included, on Chromium's event loop.
   - Each of 64 lanes has its own simulated core. Key presses come mashing (45 %), soon (40 %) or after dwelling (15 %).
   - Injected delays and faults:
     - heavy-tailed stage delays;
     - a 60 % chance that a cancelled stage drops its work (otherwise it delivers late);
     - decodes delayed 0–14 ms (5 % up to 90 ms), with 1 % failing;
     - Tier 1 late, rejected or lost;
     - 3 % of frames resent;
     - scrubs, and Esc from the core and from the panel;
     - transport stalls.
   - Beyond the Rust model: a stable partial resolves a scene before key-up whose tiers run, and the final transcript often ends the query as No match or Out of range; the Out of range offer is taken with a real click on its button, and the core answers with `scene_changed`.
   - An oracle built only from delivered messages judges every paint. An audit reads back each layer's pixel tag and pixels, and the destination chip in the DOM.

**Checks** (each must be 0):

- stale paints;
- wrong drops (a fresh frame refused);
- residue (an old, dismissed or withdrawn query's frame still on screen);
- surface mismatches;
- skip-rule overruns (past the window by more than one frame and 3 ms, once two frames have passed the deadline);
- unjustified skips;
- unshown acknowledgements (`destination_shown` while the panel did not show that chip in that key press);
- missing acknowledgements (a chip on screen for five frames unacknowledged);
- stuck frames;
- protocol errors.

**Final run of 22 Nov 2026** (22:34 CST, M4, headless shell, Chromium 158.0.8127.18, load average 12.7; `pnpm test` writes `test-results/overlay-stress-real.json`):

- **Cycles and volume.** 10,240 cycles in 64 lanes, 41.1 s of key mashing (42.9 s with launch and drain). 66,356 messages and 13,757 frames sent; 4,983 painted (Tier 0: 1,062, Tier 1: 3,136, Tier 2: 785).
- **Paths exercised.** 948 cross-fades (251 cut short), 2,086 holds (2,333 Tier 0 skips in all), 3,354 late results, 1,042 scrubs, 769 Esc presses (228 from the panel), 1,147 scenes on a stable partial (146 then No match, 186 then Out of range), 79 offers clicked (76 answered before the next press), 6,651 acknowledgements, and 4,545 layers read back.
- **Result.** All ten checks 0.
- **Timings** (`performance.now()` at 5 µs resolution; the page is cross-origin isolated):
  - message handling p50 0.015 ms, p99 0.095 ms;
  - gate check → end of draw p50 0.040 ms, p99 0.150 ms, max 0.885 ms;
  - frame arrival → draw p50 17.9 ms, p95 31.6 ms (the injected decode delay and the wait for the next frame included);
  - Tier 0 holds p50 38.4 ms, p99 95.3 ms, max 97.95 ms;
  - previous scene's frame after a scrub, until replaced: p50 25.8 ms, p99 102.5 ms, max 207.3 ms;
  - frame interval p50 and p99 16.67 ms.
- **Fade to 50 %.** A 120 ms linear fade reached 50 % opacity 83.3 ms after its first frame in this run (five frames); it has been 66.7 ms in other runs.
  - The Web Animations clock starts at the commit frame or one frame later. At 60 Hz the first frame at or past 60 ms of a fade is 66.7 ms after its start.
  - So a linear 120 ms fade cannot meet the PRD §10.1 allocation of "fade to 50 % ≤ 60". This needs an owner decision (an ease-out curve, or a shorter fade); it was not tuned away. These are page diagnostics; the measured paint is the composited frame (§10.4).

**Deliberately broken overlays** (2,400 cycles each, all ten together in 46.7 s), each caught by its designated check:

| Broken overlay | Caught by |
|---|---|
| A generation-only gate | 249 stale paints |
| Esc that only cancels | 17 stale paints |
| No tier order | 87 stale paints |
| No scene check | 150 stale paints |
| Checked at receipt only | 49 stale paints |
| Checked in one frame and drawn in the next | 171 stale paints |
| No clear on reset | 825 residue |
| A skip rule with no deadline | 17 overruns |
| No match or Out of range that keeps the scene | 28 stale paints (and 49 residue) |
| Acknowledging the destination while hidden | 31 unshown acknowledgements |

The test asserts at least 10 for stale paints, residue and unshown acknowledgements, and 5 for overruns. Esc that only cancels has its own floor of 3 (`AT_LEAST_VARIANT` in `test/stress.browser.test.ts`): it was caught 17, 15, 12 and 11 times in four full runs and 9–23 times in 16 more samples, one of them below 10, so a floor of 10 failed about one `pnpm test` in 16–20 although the broken overlay was caught every time (D-092). The real overlay must still show 0 of every violation.

**Compositing.** The browser tests use the headless shell's default compositing, not the renderer's SwiftShader GL flags (`OVERLAY_BROWSER_ARGS=swiftshader` brings them back). Measured on 22 Nov 2026 with `scripts/stress.ts`, load average 8 to 13:

- 16 lanes × 60 cycles, three runs each: the frame interval p50 was 16.67 ms in both modes. SwiftShader's tails were p95 16.7–33.3 ms, p99 66.7–116.7 ms and max 300–500 ms, with 421–439 commits; the default's p99 was 16.67 ms with 438–537 commits.
- 64 lanes × 40 cycles, two runs each: SwiftShader p95 83.3 ms, p99 133.3 ms, max 367–517 ms, 628–629 commits; the default p95 and p99 16.67 ms, 1,333–1,336 commits.

So SwiftShader costs the tails, not the median: at the stress run's 64 lanes it halves the paints, which would thin the very interleavings under test.

## Not built here

- **The frame transport** (TODO(M1), `overlay/index.ts`): how the shell serves frames under `snowglobe://localhost/` (JPEG thumbnails the webview decodes, or the Tier 0 package's decoded RGB) is to be agreed with the Tier 0 package and app/src-tauri, with `prefetch` naming the same URLs as `tier_frame_ready`.
- **The shell's IPC** (TODO(M1), `overlay/index.ts`): the `invoke` command for outbound messages and in-order inbound delivery.
- **The live-beside-simulated view** (PRD §3.2; PLAN.md puts it in v1 growth): the caption composer writes its `LIVE` line, but no message carries a live frame and the L key is not mapped.
- **The scrub bar** (TODO(M2), `overlay/view.ts`; PLAN.md M2 builds the bare slider): Left and Right already send `scrub`, and the core answers with `scene_changed`.
- **Tier 1 rendering in the webview** (TODO(M2), `overlay/scheduler.ts`): Tier 1 frames enter the same paint path through `tier_frame_ready`. When the renderer runs in the webview, it will feed the scheduler the same way.
- **IPTC metadata on refined exports** (TODO(M3), `overlay/controller.ts`): the PNG with the caption burned in is done; the core adds `trainedAlgorithmicMedia`.
