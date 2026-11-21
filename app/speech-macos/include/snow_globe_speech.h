/*
 * snow_globe_speech.h: the C ABI of Snow Globe's speech library (app/speech-macos).
 *
 * PRD §6.1 and §6.5: a Swift static library linked into the app process through a C ABI.
 * PRD §7.1 engine A1 (D-015): SpeechAnalyzer with DictationTranscriber, on device only.
 * PRD §7.2: partial transcripts arrive while the key is held, so the core can prefetch.
 * PRD §15.1 and §15.2: audio lives in RAM for one query and is never written anywhere.
 *
 * ON DEVICE ONLY. Sessions use DictationTranscriber, which Apple documents as running on the
 * device's own dictation models and as not supporting locales that SFSpeechRecognizer serves
 * only from the network. The library has no server path and no fallback to one: when the
 * on-device model cannot run, the session fails with SG_EV_ERROR. The library never downloads a
 * model (see SG_SPEECH_ASSET_NOT_INSTALLED), never records audio, and never writes audio or text
 * to disk.
 *
 * SESSION LIFECYCLE
 *   sg_speech_start      key-down: returns at once; the recognizer is set up in the background
 *                        and audio pushed meanwhile is buffered.
 *   SG_EV_READY          the recognizer is running and consuming audio (sent at most once).
 *   sg_speech_push_f32   the audio thread pushes mono float samples while the key is held.
 *   SG_EV_PARTIAL        the whole hypothesis so far (finalized text plus the tentative tail),
 *                        sent whenever it changes, before and after sg_speech_finish.
 *   SG_EV_STABLE         the stable part of that hypothesis (the finalized text, which the
 *                        recognizer never revises), sent whenever it changes (PRD §7.1 lever 2).
 *   sg_speech_finish     key-up: drains the buffered audio, adds the configured silence pad
 *                        (default none), finalizes, then sends exactly one SG_EV_FINAL with the
 *                        finalized transcript, or one SG_EV_ERROR.
 *   sg_speech_cancel     stops the session; no callback runs after it returns.
 *   sg_speech_free       cancels if needed and releases the session.
 *
 * Every session sends at most one terminal event (SG_EV_FINAL or SG_EV_ERROR); nothing follows
 * it. SG_EV_FINAL only ever follows a successful sg_speech_finish. An SG_EV_ERROR can arrive
 * before sg_speech_finish (setup failed, or the recognizer failed mid-utterance); sg_speech_finish
 * then returns SG_SPEECH_ERR_STATE and sends nothing more. If no terminal event has arrived 15 s
 * after sg_speech_finish, the session sends SG_EV_ERROR with SG_SPEECH_ERR_TIMEOUT.
 *
 * CALLBACKS
 *   They run one at a time, in order, on a serial queue owned by the session; never on the
 *   thread that called into the library. `utf8_text` is a NUL-terminated UTF-8 string that is
 *   valid only during the call (copy it to keep it); it is never NULL.
 *     SG_EV_READY    text "", audio_start_s 0, audio_end_s = seconds of audio buffered while the
 *                    recognizer was being set up.
 *     SG_EV_PARTIAL  text = the hypothesis so far; audio_start_s/audio_end_s = the recognizer's
 *                    audio range for that text (seconds since the session's first sample). The
 *                    text is "" only when the recognizer withdraws its tentative words.
 *     SG_EV_STABLE   text = the stable part of the hypothesis: the text of the recognizer's
 *                    finalized results, which it never revises; audio range of that text. Sent
 *                    whenever it changes (in practice it only grows), right after the PARTIAL for
 *                    the same result (or alone, when a tentative result was finalized unchanged),
 *                    before the terminal event. Its text is always the start (a UTF-8 byte
 *                    prefix) of the PARTIAL text that is current when it is sent, so the latest
 *                    PARTIAL and the latest STABLE together give a hypothesis and how much of it
 *                    is settled (PRD §7.1 Partial "text, stability"). Because the recognizer
 *                    never revises finalized results and reports them in time order, it is also
 *                    the start of every later PARTIAL's text and of the FINAL's text.
 *     SG_EV_FINAL    text = the finalized transcript (may be "" when nothing was recognised);
 *                    audio range of the finalized text, or 0/0 when it is empty.
 *     SG_EV_ERROR    text = a diagnostic message (never transcript text), audio_start_s 0,
 *                    audio_end_s = seconds of audio pushed so far, error_code = one of the codes
 *                    below. error_code is 0 for every other kind.
 *   Later versions may add event kinds: ignore a kind you do not know (pass `kind` through FFI
 *   as a 32-bit integer, not as a closed enum type).
 *   A callback may call sg_speech_cancel on its own session, and sg_speech_free when no other
 *   thread is inside a call on that session (see sg_speech_free). It must not block on a thread
 *   that is itself inside sg_speech_cancel or sg_speech_free for the same session.
 *
 * THREAD SAFETY
 *   sg_speech_status, sg_speech_version and the sg_speech_set_* functions: any thread.
 *     sg_speech_status blocks its caller while it asks the system (tens of milliseconds, at
 *     most 10 s); never call it on the audio thread.
 *   sg_speech_start: any thread, at any time (sessions are independent).
 *   sg_speech_finish, sg_speech_cancel: any thread, including concurrently with each other and
 *     with pushes. They never wait for the recognizer. sg_speech_cancel waits for a callback that
 *     is already running, and the first sg_speech_cancel also waits for the session's current
 *     audio step: converting one chunk of at most 4,096 samples (about 1 ms at 48 kHz).
 *   sg_speech_push_f32: one producer thread per session at a time (typically the real-time audio
 *     thread). It takes no lock, allocates nothing and makes no system call: it copies the
 *     samples into a preallocated ring buffer (at least 20 s of audio at the session's sample
 *     rate) and returns; a timer on the session's own queue drains that buffer every 5 ms. Samples
 *     pushed after sg_speech_finish or sg_speech_cancel are dropped (SG_SPEECH_ERR_STATE).
 *   sg_speech_free: call it once, when no other call on the same session is in flight on any
 *     thread (no push, finish or cancel) and none will follow; the session pointer is invalid
 *     afterwards. It may be called from inside one of the session's callbacks under the same
 *     condition.
 *
 * PRIVACY (PRD §15.1, §15.2)
 *   Audio exists only in the session's RAM. The ring buffer is zeroed as it is drained and again
 *   when the session is cancelled or freed; the library's working copies (the drain buffer, and
 *   the converter's input copy whenever a conversion takes place) are zeroed after each use. What
 *   stays unzeroed until it is released: the buffers handed to the recognizer (released with it)
 *   and the resampler's few frames of history (released when the session is cancelled or freed).
 *   Nothing is written to disk. The library never opens the microphone and never logs; callers
 *   decide what to keep (PRD §10.5: traces hold no text).
 */
#ifndef SNOW_GLOBE_SPEECH_H
#define SNOW_GLOBE_SPEECH_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct sg_speech sg_speech;  /* opaque session */

typedef enum { SG_EV_READY = 0, SG_EV_PARTIAL = 1, SG_EV_FINAL = 2, SG_EV_ERROR = 3, SG_EV_STABLE = 4 } sg_event_kind;

typedef void (*sg_speech_cb)(void *ctx, sg_event_kind kind, const char *utf8_text,
                             double audio_start_s, double audio_end_s, int32_t error_code);

/* Status codes (sg_speech_status) and error codes (return values and SG_EV_ERROR's error_code). */
#define SG_SPEECH_OK                      0
#define SG_SPEECH_ASSET_NOT_INSTALLED     1    /* the on-device model for the locale is not installed; the library never downloads it (README: the owner installs it) */
#define SG_SPEECH_NOT_AUTHORIZED          2    /* Speech Recognition is denied or restricted for this app */
#define SG_SPEECH_LOCALE_UNSUPPORTED      3    /* DictationTranscriber has no on-device model for the locale */
#define SG_SPEECH_ERR_ARGUMENT          (-1)   /* NULL or invalid argument */
#define SG_SPEECH_ERR_STATE             (-2)   /* the session has finished, failed or been cancelled */
#define SG_SPEECH_ERR_OVERFLOW          (-3)   /* the ring buffer was full; samples were dropped and the session will end in SG_EV_ERROR */
#define SG_SPEECH_ERR_AUDIO_FORMAT      (-4)   /* no usable audio format, or a conversion failed */
#define SG_SPEECH_ERR_ENGINE            (-5)   /* the recognizer failed; the text says how */
#define SG_SPEECH_ERR_TIMEOUT           (-6)   /* no answer in time (status: 10 s; finish: 15 s) */
#define SG_SPEECH_ERR_RESOURCES         (-7)   /* the system refused another simultaneous analysis */
#define SG_SPEECH_ERR_ASSET_RESERVATION (-8)   /* the model is installed but could not be reserved for this app */
#define SG_SPEECH_ERR_INTERNAL          (-9)   /* a bug in this library */

/* Model retention modes for sg_speech_set_model_retention (SpeechAnalyzer.Options.ModelRetention). */
#define SG_SPEECH_RETAIN_WHILE_IN_USE     0    /* release the model when the session ends */
#define SG_SPEECH_RETAIN_LINGERING        1    /* keep it for a while for the next session (the default) */
#define SG_SPEECH_RETAIN_PROCESS_LIFETIME 2    /* keep it until the process exits */

/* Readiness of on-device recognition for a BCP 47 locale such as "en-US". It reads the current
 * Speech Recognition authorization (SFSpeechRecognizer.authorizationStatus, which does not ask
 * the user) and asks the system whether the locale is supported and its model installed. It
 * never requests authorization, downloads a model or touches audio. Returns SG_SPEECH_OK or one
 * of the positive codes above, or SG_SPEECH_ERR_ARGUMENT or SG_SPEECH_ERR_TIMEOUT. */
int32_t sg_speech_status(const char *locale_bcp47);  /* 0 ready; 1 model asset not installed; 2 not authorized; 3 locale unsupported; <0 other */

/* Starts a session for mono audio at `sample_rate_hz` (8,000 to 384,000). Returns at once.
 * Returns NULL for a NULL or malformed locale, a sample rate out of range or a NULL callback.
 * Readiness problems found during setup (codes 1-3 and the negative codes) arrive as one
 * SG_EV_ERROR. `ctx` is passed to every callback and must stay valid until sg_speech_cancel or
 * sg_speech_free has returned. */
sg_speech *sg_speech_start(const char *locale_bcp47, double sample_rate_hz, sg_speech_cb cb, void *ctx);  /* NULL on failure; callbacks arrive on an internal queue */

/* Copies `frames` mono float samples (nominally -1..1) at the session's sample rate. Never blocks:
 * no lock, no allocation, no system call. Returns SG_SPEECH_OK, SG_SPEECH_ERR_ARGUMENT,
 * SG_SPEECH_ERR_STATE (after finish or cancel; dropped) or SG_SPEECH_ERR_OVERFLOW (dropped). */
int32_t sg_speech_push_f32(sg_speech *s, const float *mono, size_t frames);  /* copies; must not block the caller (an audio thread) for more than ~1 ms */

/* Key-up. Returns SG_SPEECH_OK when it started finishing: exactly one SG_EV_FINAL or SG_EV_ERROR
 * follows. Returns SG_SPEECH_ERR_STATE when the session had already finished, failed or been
 * cancelled (no event follows), or SG_SPEECH_ERR_ARGUMENT for NULL. Does not wait. */
int32_t sg_speech_finish(sg_speech *s);  /* key-up: flush, then exactly one FINAL (or ERROR) */

/* Stops the session and discards its audio. No callback runs after this returns, and no audio
 * reaches the recognizer after it returns. Safe to call more than once, after a terminal event,
 * and from inside a callback. */
void sg_speech_cancel(sg_speech *s);     /* no callback after this returns */

/* Cancels if needed, zeroes the session's audio buffer and releases the session. NULL is a no-op.
 * No other call on the same session may be in flight or follow (see THREAD SAFETY). */
void sg_speech_free(sg_speech *s);

/* A static, NUL-terminated description of the library build. Never NULL. */
const char *sg_speech_version(void);

/* Process-wide settings, read by sessions started after the call (PRD §7.1). Thread-safe.
 * The library has no other inputs: it reads no environment variables and no files.
 *
 * Contextual strings (viewpoint names and aliases) bias recognition towards those phrases. The
 * strings are copied; count 0 clears them. Returns SG_SPEECH_ERR_ARGUMENT for a NULL list with
 * count > 0, a NULL, empty, blank (whitespace only) or non-UTF-8 string, or more than 1,000
 * strings; a rejected call changes nothing. */
int32_t sg_speech_set_contextual_strings(const char *const *utf8_strings, size_t count);

/* Silence pushed as one burst after the last sample at key-up, before finalizing (PRD §7.1
 * finalization lever 1, to be measured). 0 (the default) to 2,000 ms; else SG_SPEECH_ERR_ARGUMENT. */
int32_t sg_speech_set_silence_pad_ms(int32_t ms);

/* How long the recognizer's model stays loaded after a session: SG_SPEECH_RETAIN_*. The default is
 * SG_SPEECH_RETAIN_LINGERING. Other values return SG_SPEECH_ERR_ARGUMENT. */
int32_t sg_speech_set_model_retention(int32_t retention);

#ifdef __cplusplus
}
#endif

#endif /* SNOW_GLOBE_SPEECH_H */
