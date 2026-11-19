# Hosted few-step refinement (Tier 2) — landscape, 18 Nov 2026

Desk research by a research agent from model cards, provider docs and pricing pages; **every latency figure is a vendor claim, none includes our network round trip, and nothing has been benchmarked by us.** No accounts, no paid calls. Feeds DECISIONS.md D-006; the decision comes from the M3 benchmark on our own plates.

## The key distinction

- **SDEdit-style img2img** (start from our frame + a little noise, denoise a few steps) has a true **strength** knob and is **pixel-aligned by construction**; the realism–faithfulness trade-off is monotonic in noise level [S3]. This is what "refine at low strength, keep weather/lighting/geometry" needs.
- **Reference-conditioned editors** (FLUX.2 edit, Kontext, Qwen-Image-Edit, Nano Banana, GPT-image, Seedream) regenerate every pixel from noise with the input as a reference. No strength parameter; unedited regions come back "shifted, zoomed, offset" [Q1]. Diffusers maintainers confirm FLUX.2 pipelines don't support noisy-latent img2img [S2].
- **FLUX.1 img2img is a poor fit:** on guidance-distilled FLUX, strengths below ~0.8 have "minimal to no visible effect" [R4].

## Candidates

| Model / provider | Mode | Price | Latency (vendor) | Notes |
|---|---|---|---|---|
| **Z-Image-Turbo** (6B, Apache-2.0) · **fal** `z-image/turbo/image-to-image` | img2img, `strength` (default 0.6), 1–8 steps, `acceleration` none/regular/high, `sync_mode` returns a data URI, not stored [F1] | $0.005/MP | none published | — |
| Z-Image-Turbo · fal ControlNet | canny/depth/pose, `control_scale`, start/end [F2] | $0.0065/MP | — | **No init image**, so colour and light come from the prompt, not our render. Wrong tool. |
| Z-Image-Turbo · **Runware** | `seedImage` + `strength` (default 0.8) + optional ControlNet; URL/data URI/base64 [R1] | from $0.0012 per run | ~2 s at 1280×768, ~4 s at 1024² (8 steps), measured end to end by Runware [R1] | "never" trains on API data; ZDR on request [R3]; $2 credit with a business email |
| Z-Image-Turbo · Replicate (community Pruna build) | img2img | ~$0.066/run | "typically within 44 s" [P1] | Avoid |
| **FLUX.2 [klein] 4B** (distilled, 4 steps, Apache-2.0) · BFL direct | reference edit | $0.014 first MP + $0.001/extra MP + $0.001/MP per reference [B1] | ~0.3 s inference on GB200 [B2] | BFL privacy policy (rev. 2026-08-01) **may train on prompts, images and outputs**, email opt-out [B4] |
| FLUX.2 klein 4B · **Prodia** | "img2img" job type | from $0.003/image, rising with resolution and inputs | ETA ~0.5 s [D1] | inputs ≤ 1920², ≤ 8; data policy not verified |
| FLUX.2 klein 4B · **fal** edit (HTTP) | reference edit, ≤ 4 inputs, custom `image_size` [F3] | $0.01/MP | — | — |
| FLUX.2 klein 4B · fal `/realtime` WebSocket | reference edit | $0.00194 per compute-second | bypasses queue; first connection may cold-start [F5] | data-URI input (704² JPEG recommended); **square output only (768/1024)** [F4] |
| FLUX.2 klein 4B · Runware | reference-to-image | $0.0019 | ~6 s at 1024² [R2] | — |
| FLUX.2 klein 4B · Cloudflare Workers AI | — | ~$0.002 per 1280×720 frame; 10k free neurons/day ≈ 60 frames; ZDR | — | **inputs must be < 512×512** — blurs bridge cables [C1, C2] |
| FLUX 3 Image Klein (2026-10-15) · BFL | edit | — | median 4.54 s t2i, **7.18 s edit** [B3] | FLUX Kommunity licence |
| Kontext [dev] · fal | edit | $0.025/MP | ~9 s (third party) [S6] | non-commercial weights |
| Qwen-Image-Edit-2511 · fal | edit | $0.03/MP | — | ~8 px drift from resizing [Q1]; weak consistency in EdiVal [S5] |
| Nano Banana 2 Lite / 2.1 · Google | edit | $0.0336 per 1K image | ~4 s [G1, G2] | paid tier not used for training; no free tier for image |
| GPT-image-2 · OpenAI | edit | $8 / $30 per M image tokens | 15–40 s (reseller) [O1] | — |
| Seedream 5.0 Flash · edit | edit | — | 23–28 s [S1] | — |
| P-Image-Edit · Replicate | edit | ~$0.01 | < 1 s [P2] | base model and licence not stated |
| SDXL-Turbo / LCM · fal `/realtime` | img2img with strength | price page shows a $0 placeholder | "sub-100 ms" claims [F5] | not verified |

Not researched in depth: HiDream, Stability (SD3.5 Large Turbo ≈ $0.04/image per aggregators).

## Structure preservation (what is known)

- Z-Image img2img strength tests [S4]: 0.15–0.25 layout "nearly unchanged"; 0.30–0.45 structure holds while style shifts; above ~0.55 backgrounds reflow.
- **Few-step caveat:** at 8 steps, strength 0.25 runs only ~2 denoising steps, so the realism gain may be small. The benchmark has to measure *realism gained* as well as drift.
- No published benchmark measures render-to-photo preservation of *weather and lighting*. Our automatic check is the real test. Reference editors silently changing sky or light is plausible but unverified.

## Providers: transport and data

- **fal:** `/realtime` bypasses the queue, but the first connection may cold-start [F5]. `ws.fal.run` is **retired 2026-11-20** [F6]. Payloads are stored 90 days by default; the `X-Fal-Store-IO: 0` header opts out, and CDN lifetime can be set per request [F7]. Privacy policy (2026-07-22) says nothing about training. New accounts are limited to **2 concurrent requests** [F8]; free credit not verifiable.
- **Replicate:** official models always warm [P3]; API inputs/outputs deleted after 1 h [P4]; data URIs recommended only under 1 MB.
- **Runware:** never trains on API data; ZDR on request [R3].
- **BFL direct:** may train on inputs unless you opt out by email [B4].
- Prodia, Together: data policy not verified.

## Licensing

- Z-Image-Turbo (+ ControlNet) and FLUX.2 klein 4B: **Apache-2.0**, no restrictions [B2, S7].
- FLUX Non-Commercial licence (FLUX.1 dev, Kontext dev, FLUX.2 dev, klein 9B): outputs usable "for any purpose", but "non-commercial purpose" excludes use "in direct interactions with or that has impact on end users", so self-hosting those weights in a public app needs a commercial licence. Via BFL's API, commercial rights are included; via other hosts, the host's terms apply. The licence requires an "AI-generated/modified" disclosure, so refined frames are labelled (we label them anyway) [S8, B5].
- FLUX 3 Klein (Kommunity licence): free commercially under $5M revenue [B3].

## Shortlist to benchmark (agent's estimates, built on vendor claims)

1. **fal · Z-Image-Turbo img2img**, strength 0.15–0.35, acceleration high, `sync_mode`. Expected ~0.8–1.5 s warm; ~$0.005 per 1280×720 frame (~$0.003 at 1024×576). The only true low-strength, pixel-aligned path; cheapest; Apache-2.0.
2. **Prodia · FLUX.2 klein 4B img2img.** Expected ~0.8–1.3 s; ~$0.004–0.006. Best photoreal quality at the lowest published latency; drift and data policy are the open questions.
3. **fal · FLUX.2 klein 4B edit (HTTP)**, with a small side test of `/realtime`. Expected ~1–2 s; ~$0.01–0.02. Same model as #2, so it separates provider latency from model latency.

Runware's Z-Image-Turbo is a strong alternative to #1 if fal's tail latency is poor (measured ~2 s end to end at 1280×768; no-training policy).

## Benchmark cost estimate

3 candidates × 3 plates × 6 conditions × 5 repeats = 270 calls: #1 ≈ $0.45, #2 ≤ $0.54, #3 ≤ $1.80 → **core ≈ $2–3**; with a ×4 strength sweep on #1, cold-start probes and 2× retry headroom → **≈ $6–8**.

## Biggest risk to a 2 s refine budget

**p95 tail latency from shared serverless infrastructure, not GPU time.** For the same klein 4B model, vendor figures range from 0.3 s (BFL inference) to ~6 s (Runware end to end). Cold starts, queueing, two network legs and fal's 2-concurrent-request cap on new accounts dominate. Mitigations: send only the latest frame and cancel superseded calls; 1024×576 JPEG data URI with `sync_mode`; pre-warm and keep the connection open; measure p95 from the real deployment region.

## Sources (accessed 2026-11-18)

F1 fal.ai/models/fal-ai/z-image/turbo/image-to-image/llms.txt · F2 fal.ai/models/fal-ai/z-image/turbo/controlnet/llms.txt · F3 fal.ai/models/fal-ai/flux-2/klein/4b/edit/llms.txt · F4 fal.ai/models/fal-ai/flux-2/klein/realtime/llms.txt · F5 fal.ai/docs/model-apis/real-time · F6 fal.ai/docs/documentation/model-apis/inference/websockets.md · F7 fal.ai/docs/documentation/model-apis/media-expiration.md; fal.ai/privacy (2026-07-22) · F8 fal.ai/docs/documentation/model-apis/faq.md (via search) · B1 bfl.ai/pricing · B2 bfl.ai/models/flux-2-klein · B3 bfl.ai/blog/flux-3-image-klein (2026-10-15) · B4 bfl.ai/legal/privacy-policy (2026-08-01) · B5 help.bfl.ai/articles/9272590838 · D1 docs.prodia.com/models/flux-2-klein/ · R1 runware.ai/docs/models/alibaba-z-image-turbo · R2 runware.ai/docs/models/bfl-flux-2-klein-4b · R3 runware.ai/docs/platform/zero-data-retention · R4 runware.ai/docs/image-inference/image-to-image · C1 developers.cloudflare.com/workers-ai/platform/pricing/ (2026-11-18) · C2 developers.cloudflare.com/changelog/post/2026-01-15-flux-2-klein-4b-workers-ai/ · P1 replicate.com/prunaai/z-image-turbo-img2img · P2 replicate.com/prunaai/p-image-edit · P3 replicate.com/docs/topics/models/official-models.md · P4 replicate.com/docs/topics/predictions/data-retention.md · G1 aistudio.google.com/docs/pricing · G2 the-decoder.com (NB2 Lite, 2026-06-30) · O1 developers.openai.com/api/docs/pricing; runflow.io/models/openai/gpt-image-2/edit · Q1 lilting.ch/en/articles/qwen-image-edit-pixel-perfect-referencelatent; comfy.icu/node/LockPixelQwenEncode · S1 atlascloud.ai/blog/tips/seedream-5.0-flash-vs-pro · S2 huggingface.co/black-forest-labs/FLUX.2-klein-9B/discussions/36 · S3 arxiv.org/abs/2108.01073 · S4 wavespeed.ai/blog/posts/blog-z-image-turbo-img2img-strength-denoise (2026-01-21) · S5 alphaxiv.org/abs/2509.13399; arxiv.org/pdf/2602.14068 · S6 fal.ai/models/fal-ai/flux-kontext/dev/llms.txt; segmind.com/models/flux-kontext-dev/pricing · S7 huggingface.co/alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union · S8 huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md
