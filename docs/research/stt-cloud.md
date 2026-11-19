# Cloud streaming STT and LLM fallback — landscape, 18 Nov 2026

Desk research by a research agent from vendor docs and pricing pages plus one independent benchmark; **nothing here has been benchmarked by us yet.** No accounts created, no paid calls. "undated" = the page was read on 18 Nov 2026 and shows no date. These notes feed DECISIONS.md D-004 and D-005; the decision itself comes from our replay harness in M1.

Only independent latency source: the **Pipecat STT benchmark** [P] (run 2026-08-26, 1,000 clips, one pipeline). It reports **TTFS** = time from end of speech to final transcript. It does not state whether it used force-finalize messages, so our explicit-finalize numbers may differ.

## Streaming STT

| Service | Finalize control | Vocabulary biasing | Independent latency (TTFS P50/P95/P99) | Price | Data |
|---|---|---|---|---|---|
| **Deepgram Nova-3** (WS `/v1/listen`) | `{"type":"Finalize"}` → `from_finalize:true` (docs: not guaranteed when little audio is buffered; no latency figure) [D1] | Keyterm prompting, up to 500 tokens, updatable mid-stream [D2] | **247 / 298 / 326 ms**, 1.62 % WER — lowest and most consistent hosted [P] | $0.0048/min (promo; was $0.0077), keyterms +$0.0013/min; per-second billing, no minimum; $200 non-expiring credit [D3] | Model-improvement program by default; opt out per request with `mip_opt_out=true` [D4] |
| **Deepgram Flux** (`/v2/listen`) | `{"type":"ForceEndTurn"}`; `eot_threshold=1.0` disables its own end-of-turn detection [D5] | Keyterms (as above) | not in [P] | $0.0065/min [D3] | as above |
| **AssemblyAI STT-3.7 Realtime-Agent** (launched 2026-11-17/18) | `{"type":"ForceEndpoint"}` (manual mode, this model only); `Terminate` on all models [A3] | Up to 100 keyterms, updatable [A3] | Vendor only: 86 ms final after end of speech, 493 ms first partial [A1]. Predecessor universal-3-6-pro: best hosted WER in [P] (0.96 %), 307 ms P50 | $0.45/h incl. keyterms; **billed on WebSocket open time** (rounding unit unpublished); $50 credit [A4] | Zero retention for streaming only if opted out of training; **free accounts cannot opt out** [A5] |
| **Soniox stt-rt-v6** (2026-11-03, "faster voice typing and dictation") [S1] | `{"type":"finalize"}` → final tokens + `<fin>` marker; no published latency [S2] | Context incl. terms list, up to 8k tokens [S3] | v5: 260 / 305 / 313 ms, 1.27 % WER (on the accuracy/latency frontier) [P]; v6 not benchmarked | $0.12/h (~$0.002/min), billed on stream duration [S2, S4]; free credit not verified | Real-time audio not stored, never used for training [S5] |
| **OpenAI gpt-live-transcribe** (2026-07-28) [O2] | Realtime WS `intent=transcription`, no server VAD; client sends `input_audio_buffer.commit` [O1] | `keywords`, `prompt`; `delay` minimal…xhigh [O1] | No vendor ms figures; older OpenAI models 637–740 ms P50 in [P] | $0.017/min; batch gpt-transcribe $0.0045/min [O3] | Not used for training; 30-day abuse logs on `/v1/realtime`; ZDR needs approval [O4] |
| ElevenLabs Scribe v3 Live (2026-11-10) | chunk with `"commit":true` [E1] | realtime keyterm limits undocumented | v2 Realtime: 3.12 % WER, 281 ms [P] | $0.22/h + $0.08/h keyterms [E3] | ZDR enterprise-only [E2] |
| Speechmatics Linden-1 | `ForceEndOfUtterance` not listed for Linden [SM2] | — | 1.05 % WER, 369 ms P50 [P] | $0.30/h promo, $100 credit, per-second [SM1] | — |
| Cartesia Ink-2 | — | — | 299 ms P50 but **1,584 ms P99** [P] | — | English only |
| Gladia Solaria-1 | — | — | — | €0.68/h | self-serve trains by default [G1] |
| Google STT V2 / gemini-3.5-transcribe-live | — | — | gemini live 458 ms P50 [P]; Chirp 3 untested | $0.016/min, per-second [GG1] | — |
| Groq whisper-large-v3-turbo (batch on release) | n/a — upload whole clip at key-up | prompt ≤ 224 tokens [GQ1] | ~180 ms median for a 5 s clip, single blog, unverified [GQ2] | $0.04/h, **10 s minimum per request** [GQ1] | — |

**What this means for the Tier 0 budget.** The best independently measured hosted p95 from end of speech to final transcript is ~300 ms — the whole 300 ms Tier 0 budget. Explicit finalize on key release may beat VAD-based endpointing (we know exactly when speech ends), but that is unmeasured. Tier 0 therefore cannot rely on waiting for a cloud final; it needs either an on-device engine that finalizes faster, or acting on a stable partial and confirming with the final.

## LLM fallback parser

| Option | Price (in/out per M tokens) | Latency evidence | Notes |
|---|---|---|---|
| **Claude Haiku 5.5** (`claude-haiku-5-5`) | $0.10 / $0.50 [K1] | 0.59 s TTFT, 183 tok/s with thinking off, low effort (one test, median of 3, Oct 2026) [K2] → ~0.9 s for 60 tokens. Artificial Analysis lists 5.8–9 s TTFT, but that includes thinking [K3] | Disable thinking for this route |
| **Groq gpt-oss-20b** | $0.075 / $0.30 [GQ3] | ~1,000 tok/s; no credible short-prompt TTFT found | Strict JSON schema, not while streaming [GQ4]; reasoning model, set effort low |
| Gemini 2.5 Flash-Lite | $0.10 / $0.40 [GM1] | 0.35 s TTFT from blogs only, unverified | not deprecated [GM2] |
| *Apple Foundation Models (on-device)* | $0 | see `stt-ondevice-macos.md` | — |

## Shortlist and cost

- **Cloud STT to benchmark:** (1) Deepgram Nova-3 with Finalize, plus Flux with ForceEndTurn on the same account; (2) Soniox stt-rt-v6; (3) AssemblyAI STT-3.7 Realtime-Agent (one day old; verify everything).
- **LLM fallback:** Claude Haiku 5.5 (thinking disabled); Groq gpt-oss-20b (low reasoning effort).
- **Benchmark cost estimate:** 30 utterances × 10 passes × 3 providers ≈ 900 sessions × ~5 s billed: Deepgram ≈ $0.15, Soniox ≈ $0.05, AssemblyAI ≈ $0.19, Haiku parse calls ≈ $0.05 → **≈ $0.45**; worst case ~$3 if sessions round up to a minute (unverified). Deepgram's and AssemblyAI's sign-up credits cover their share.

## Sources

- [P] https://www.pipecat.ai/benchmarks/speech-to-text (run 2026-08-26)
- [D1] https://developers.deepgram.com/docs/finalize · [D2] https://developers.deepgram.com/docs/keyterm · [D3] https://deepgram.com/pricing · [D4] https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program · [D5] https://developers.deepgram.com/docs/flux/force-end-turn (summary of page + Flux quickstart)
- [A1] https://www.assemblyai.com/llms/models.md (2026-11-17) · [A2] https://assemblyai.com/changelog (2026-11-18) · [A3] https://assemblyai.com/docs/streaming/api-spec/streaming-websocket · [A4] https://www.assemblyai.com/pricing (2026-11-10) · [A5] https://assemblyai.com/docs/data-retention-and-model-training, https://assemblyai.com/docs/data-controls (via search summaries)
- [S1] https://docs.soniox.com/stt/models · [S2] https://docs.soniox.com/stt/rt/manual-finalization · [S3] https://soniox.com/docs/stt/concepts/context (summary) · [S4] https://soniox.com/pricing (June 2026) · [S5] https://soniox.com/docs/stt/security-and-privacy (summary)
- [O1] https://developers.openai.com/api/docs/guides/realtime-transcription · [O2] https://community.openai.com/t/gpt-live-transcribe-and-gpt-transcribe-two-new-transcription-models-in-the-api/1388318 (2026-07-29) · [O3] https://developers.openai.com/api/docs/pricing · [O4] https://developers.openai.com/api/docs/guides/your-data
- [E1] https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime · [E2] https://elevenlabs.io/blog/scribe-v3 (2026-11-14) · [E3] https://elevenlabs.io/pricing/api
- [SM1] https://www.speechmatics.com/pricing · [SM2] https://docs.speechmatics.com/rt-api-ref
- [G1] https://www.gladia.io/pricing · [GG1] https://cloud.google.com/speech-to-text/pricing
- [GQ1] https://console.groq.com/docs/speech-to-text · [GQ2] https://dev.to/howmindswork/groq-vs-openai-whisper-real-benchmarks-for-voice-transcription-2026-46lk (2026-05-07) · [GQ3] https://console.groq.com/docs/models · [GQ4] https://console.groq.com/docs/structured-outputs
- [K1] https://platform.claude.com/docs/en/about-claude/models/overview · [K2] https://computingforgeeks.com/claude-haiku-5-5-released-features-benchmarks/ (2026-10-08) · [K3] https://artificialanalysis.ai/models/claude-haiku-5-5
- [GM1] https://aistudio.google.com/docs/pricing · [GM2] https://aistudio.google.com/docs/deprecations
