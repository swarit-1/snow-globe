@preconcurrency import AVFoundation
import Dispatch
import Foundation
import LookoutSpeechC
import Synchronization

/// The caller's callback and context. The context is the caller's to keep valid (until
/// `lk_speech_cancel` or `lk_speech_free` returns); the library only hands it back.
struct CallbackTarget: @unchecked Sendable {
    let function: lk_speech_cb
    let context: UnsafeMutableRawPointer?
}

/// Moves a value that is only ever used on one queue at a time into a `@Sendable` closure.
struct QueueHandoff<T>: @unchecked Sendable {
    let value: T
}

/// One speech session behind an `lk_speech *` (PRD §7.1 `SpeechSession`, §6.3 steps 1-3).
///
/// Threads and queues:
/// - **push path** (`push`): the caller's audio thread. Only atomics and the lock-free ring.
/// - **work queue** (serial): a 5 ms timer drains the ring, converts and feeds the engine. The
///   converter, the drain timer and the scratch buffer are confined to it.
/// - **callback queue** (serial): the only place the caller's callback runs, in emission order.
/// - **tasks**: setup (`prepare`, `start`), finish, and the engine's own result stream.
/// - **control** (a mutex): the state machine and the transcript. Every event is decided and
///   queued under it, so events go out in the order they were decided, and the terminal-event
///   rule (at most one FINAL or ERROR, nothing after it) holds across all of these threads.
///
/// Cancellation: `cancel` sets `cancelled` under the mutex, and every queued callback checks it
/// right before calling out. `cancel` then waits for the callback queue (unless it is called from
/// a callback), so no callback starts or runs after it returns.
package final class Session: @unchecked Sendable {
    /// The ring holds at least this many seconds at the session's rate (audio pushed while the
    /// recognizer is still being set up waits here).
    static let bufferSeconds = 20.0
    /// How often the work queue drains the ring.
    static let drainIntervalMs = 5
    /// The smallest chunk fed while the key is held (at key-up everything left is fed).
    static let minimumFeedSeconds = 0.010

    let locale: String
    let sampleRate: Double
    let settings: SessionSettings
    package let ring: FloatRing
    private let target: CallbackTarget
    private let engine: any RecognizerEngine
    private let id: UInt64
    private let workQueue: DispatchQueue
    private let callbackQueue: DispatchQueue

    private static let nextID = Atomic<UInt64>(1)
    private static let callbackQueueKey = DispatchSpecificKey<UInt64>()

    // Push path.
    private let accepting = Atomic<Bool>(true)
    /// Set by the first refused push and never cleared: the session then ends in an ERROR.
    private let overflowed = Atomic<Bool>(false)
    /// Frames refused because the ring was full, saturating at Int.max (for the ERROR's text).
    private let droppedFrames = Atomic<Int>(0)

    private struct Control {
        var cancelled = false
        var terminalSent = false
        var finishRequested = false
        var ready = false
        var assembler = TranscriptAssembler()
        var lastPartialText = ""
        var lastStableText = ""
        var setupTask: Task<Void, Never>?
        var watchdog: (any DispatchSourceTimer)?
        /// The engine has been told to cancel (it is told once).
        var engineCancelled = false
    }

    private let control = Mutex(Control())

    // Work-queue confined.
    private var converter: PCMConverter?
    private var drainTimer: (any DispatchSourceTimer)?
    private let scratchFrames = PCMConverter.chunkFrames
    private let scratch: UnsafeMutablePointer<Float>
    private var closed = false

    init(
        locale: String, sampleRate: Double, target: CallbackTarget, settings: SessionSettings,
        engine: any RecognizerEngine
    ) {
        self.locale = locale
        self.sampleRate = sampleRate
        self.target = target
        self.settings = settings
        self.engine = engine
        id = Self.nextID.wrappingAdd(1, ordering: .relaxed).oldValue
        ring = FloatRing(minimumCapacity: Int((sampleRate * Self.bufferSeconds).rounded(.up)))
        scratch = .allocate(capacity: scratchFrames)
        scratch.initialize(repeating: 0, count: scratchFrames)
        workQueue = DispatchQueue(label: "dev.lookout.speech.work", qos: .userInitiated)
        callbackQueue = DispatchQueue(label: "dev.lookout.speech.callbacks", qos: .userInitiated)
        callbackQueue.setSpecific(key: Self.callbackQueueKey, value: id)
    }

    deinit {
        scratch.deallocate()
    }

    /// Frames accepted by `push` so far.
    package var framesPushed: Int { ring.totalWritten }

    /// Frames `push` dropped because the ring was full (saturating).
    package var framesDropped: Int { droppedFrames.load(ordering: .acquiring) }

    /// True once any push was refused because the ring was full.
    package var didOverflow: Bool { overflowed.load(ordering: .acquiring) }

    // MARK: Start

    /// Starts the drain timer and the asynchronous setup; returns at once.
    func start() {
        workQueue.async { [self] in
            let t = DispatchSource.makeTimerSource(queue: workQueue)
            let interval = DispatchTimeInterval.milliseconds(Self.drainIntervalMs)
            t.schedule(deadline: .now() + interval, repeating: interval, leeway: .milliseconds(1))
            t.setEventHandler { [weak self] in self?.drain(final: false) }
            drainTimer = t
            t.resume()
        }
        control.withLock { c in
            c.setupTask = Task.detached(priority: .userInitiated) { [self] in await runSetup() }
        }
    }

    private func runSetup() async {
        let config = EngineConfig(
            locale: locale, inputSampleRate: sampleRate, contextualStrings: settings.contextualStrings,
            retention: settings.retention)
        do {
            let format = try await engine.prepare(config)
            let converter = try PCMConverter(inputSampleRate: sampleRate, output: format)
            try await engine.start(
                onResult: { [weak self] r in self?.handle(r) },
                onFailure: { [weak self] e in self?.fail(e) })
            // READY is queued before the converter is installed, so it precedes every PARTIAL.
            let proceed = control.withLock { c -> Bool in
                guard !c.cancelled, !c.terminalSent else { return false }
                c.ready = true
                let buffered = Double(ring.available) / sampleRate
                enqueue(&c, LK_EV_READY, "", 0, buffered, LK_SPEECH_OK, terminal: false)
                return true
            }
            guard proceed else {
                // Cancelled or failed during setup: whoever did that has told the engine already.
                return
            }
            let handoff = QueueHandoff(value: converter)
            workQueue.async { [self] in
                // A cancel that landed after READY was decided has already cleared the work
                // queue's state; don't bring a converter (and its resampler) back.
                guard !closed, !control.withLock({ $0.cancelled }) else { return }
                self.converter = handoff.value
                drain(final: false)
            }
        } catch {
            fail(ErrorMapping.map(error))
        }
    }

    // MARK: Push (audio thread)

    /// Copies samples into the ring. Lock-free and allocation-free (see `FloatRing.write`).
    ///
    /// A refused push sets a flag that is never cleared, so the session ends in an ERROR however
    /// large or many the refused pushes were (a counter alone could wrap).
    @inline(__always)
    func push(_ samples: UnsafePointer<Float>, _ count: Int) -> Int32 {
        guard accepting.load(ordering: .acquiring) else { return LK_SPEECH_ERR_STATE }
        if ring.write(samples, count: count) { return LK_SPEECH_OK }
        overflowed.store(true, ordering: .releasing)
        // One producer per session (the header's rule), so a load and a store suffice.
        let (sum, wrapped) = droppedFrames.load(ordering: .relaxed).addingReportingOverflow(count)
        droppedFrames.store(wrapped ? Int.max : sum, ordering: .relaxed)
        return LK_SPEECH_ERR_OVERFLOW
    }

    // MARK: Work queue

    /// Moves audio from the ring to the engine. While the key is held it waits for at least
    /// `minimumFeedSeconds`; with `final` it takes everything.
    private func drain(final: Bool) {
        guard !closed, let converter else { return }
        let (cancelled, terminal) = control.withLock { ($0.cancelled, $0.terminalSent) }
        if cancelled || terminal {
            ring.discardAll()
            return
        }
        let minimum = final ? 1 : max(1, Int(sampleRate * Self.minimumFeedSeconds))
        while ring.available >= minimum {
            // A failure or a cancel can land while this loop runs (the engine reports from
            // inside `feed`); stop feeding at once rather than after the buffered audio.
            if control.withLock({ $0.cancelled || $0.terminalSent }) {
                ring.discardAll()
                return
            }
            let n = ring.read(into: scratch, maxCount: scratchFrames)
            guard n > 0 else { break }
            defer { scratch.update(repeating: 0, count: n) }
            do {
                for b in try converter.convert(scratch, count: n) {
                    // The engine can report a failure from inside `feed`.
                    if hasEnded() {
                        ring.discardAll()
                        return
                    }
                    engine.feed(b)
                }
            } catch {
                fail(ErrorMapping.map(error))
                ring.discardAll()
                return
            }
        }
    }

    private func stopDrainTimer() {
        drainTimer?.cancel()
        drainTimer = nil
    }

    /// Key-up on the work queue: the last samples, the silence pad, then the resampler's tail.
    /// It checks for a cancel or a failure before every chunk, so a cancel waits for one chunk at
    /// most (the header's promise), and nothing is fed once the session has ended.
    private func finalDrain() -> EngineError? {
        stopDrainTimer()
        guard let converter else { return EngineError(LK_SPEECH_ERR_INTERNAL, "no converter at finish") }
        drain(final: true)
        // The drain may have failed the session, or a cancel may have landed: feed nothing more.
        if hasEnded() {
            return EngineError(LK_SPEECH_ERR_STATE, "the session ended during the final drain")
        }
        do {
            let padFrames = Int((Double(settings.silencePadMs) / 1_000 * sampleRate).rounded())
            let engine = engine
            let padded = try converter.convertSilence(
                frames: padFrames, shouldStop: { hasEnded() }, deliver: { b in if !hasEnded() { engine.feed(b) } })
            if !padded || hasEnded() {
                return EngineError(LK_SPEECH_ERR_STATE, "the session ended during the silence pad")
            }
            if let tail = try converter.flush() { engine.feed(tail) }
        } catch {
            return ErrorMapping.map(error)
        }
        return nil
    }

    /// True once the session was cancelled or sent its terminal event.
    private func hasEnded() -> Bool {
        control.withLock { $0.cancelled || $0.terminalSent }
    }

    // MARK: Results

    /// One recognizer result: a PARTIAL when the hypothesis changed, then a STABLE when its
    /// finalized part changed (header: LK_EV_STABLE). Both come from one ordered list under this
    /// lock, so a STABLE is a prefix of the PARTIAL text current when it is sent. PARTIAL goes
    /// first because, under the recognizer's rules (finals never revised, in time order), the old
    /// STABLE is a prefix of the new PARTIAL, while a new STABLE need not be a prefix of the old
    /// PARTIAL (a final can replace different tentative words). Texts are compared byte for byte
    /// (String equality would treat two Unicode normalizations as equal).
    private func handle(_ r: RecognizerResult) {
        control.withLock { c in
            guard !c.cancelled, !c.terminalSent else { return }
            c.assembler.apply(r)
            let snap = c.assembler.current
            if !Self.sameBytes(snap.text, c.lastPartialText) {
                c.lastPartialText = snap.text
                enqueue(&c, LK_EV_PARTIAL, snap.text, snap.start, snap.end, LK_SPEECH_OK, terminal: false)
            }
            let stable = c.assembler.stable
            if !Self.sameBytes(stable.text, c.lastStableText) {
                c.lastStableText = stable.text
                enqueue(&c, LK_EV_STABLE, stable.text, stable.start, stable.end, LK_SPEECH_OK, terminal: false)
            }
        }
    }

    private static func sameBytes(_ a: String, _ b: String) -> Bool {
        a.utf8.elementsEqual(b.utf8)
    }

    // MARK: Finish

    /// Key-up: stops accepting audio and finishes in the background (see the header).
    func finish() -> Int32 {
        let accepted = control.withLock { c -> Bool in
            guard !c.cancelled, !c.terminalSent, !c.finishRequested else { return false }
            c.finishRequested = true
            c.watchdog = makeWatchdog()
            return true
        }
        guard accepted else { return LK_SPEECH_ERR_STATE }
        accepting.store(false, ordering: .releasing)
        Task.detached(priority: .userInitiated) { [self] in await runFinish() }
        return LK_SPEECH_OK
    }

    private func runFinish() async {
        let setup = control.withLock { $0.setupTask }
        await setup?.value
        guard control.withLock({ !$0.cancelled && !$0.terminalSent && $0.ready }) else { return }
        let drainError: EngineError? = await withCheckedContinuation { cont in
            workQueue.async { [self] in
                cont.resume(returning: closed ? EngineError(LK_SPEECH_ERR_STATE, "session closed") : finalDrain())
            }
        }
        if let e = drainError {
            fail(e)
            return
        }
        if overflowed.load(ordering: .acquiring) {
            let dropped = droppedFrames.load(ordering: .acquiring)
            fail(EngineError(
                LK_SPEECH_ERR_OVERFLOW,
                "\(dropped) frames were dropped because the ring buffer was full; the transcript would have a gap"))
            return
        }
        do {
            try await engine.finish()
        } catch {
            fail(ErrorMapping.map(error))
            return
        }
        control.withLock { c in
            guard !c.cancelled, !c.terminalSent else { return }
            c.watchdog?.cancel()
            c.watchdog = nil
            let f = c.assembler.finalTranscript
            enqueue(&c, LK_EV_FINAL, f.text, f.start, f.end, LK_SPEECH_OK, terminal: true)
        }
    }

    private func makeWatchdog() -> any DispatchSourceTimer {
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        let seconds = settings.finishTimeoutSeconds
        t.schedule(deadline: .now() + seconds)
        t.setEventHandler { [weak self] in
            self?.fail(EngineError(
                LK_SPEECH_ERR_TIMEOUT, "no final transcript \(seconds) s after lk_speech_finish"))
        }
        t.resume()
        return t
    }

    // MARK: Failure and events

    /// Sends one ERROR (if no terminal event went out yet) and stops the session's work.
    func fail(_ e: EngineError) {
        let sent = control.withLock { c -> Bool in
            guard !c.cancelled, !c.terminalSent else { return false }
            c.watchdog?.cancel()
            c.watchdog = nil
            // Close the push path before the ERROR can reach the caller.
            accepting.store(false, ordering: .releasing)
            let pushed = Double(ring.totalWritten) / sampleRate
            enqueue(&c, LK_EV_ERROR, e.detail, 0, pushed, e.code, terminal: true)
            c.engineCancelled = true
            return true
        }
        guard sent else { return }
        workQueue.async { [self] in
            stopDrainTimer()
            ring.discardAll()
        }
        Task.detached { [engine] in await engine.cancel() }
    }

    /// Queues one callback. Called with the control mutex held, so queue order is decision order.
    private func enqueue(
        _ c: inout Control, _ kind: lk_event_kind, _ text: String, _ start: Double, _ end: Double,
        _ code: Int32, terminal: Bool
    ) {
        if terminal { c.terminalSent = true }
        let target = target
        callbackQueue.async { [self] in
            guard !control.withLock({ $0.cancelled }) else { return }
            text.withCString { target.function(target.context, kind, $0, start, end, code) }
        }
    }

    // MARK: Cancel and close

    /// No callback runs, and nothing is fed to the engine, after this returns (see the type's
    /// notes). The first cancel waits for the work queue's current item: one drain or pad chunk
    /// (`finalDrain` checks for a cancel before each chunk), then clears the work queue's state.
    func cancel() {
        let (first, cancelEngine) = control.withLock { c -> (Bool, Bool) in
            let was = c.cancelled
            c.cancelled = true
            c.watchdog?.cancel()
            c.watchdog = nil
            let tellEngine = !c.engineCancelled
            c.engineCancelled = true
            return (!was, tellEngine)
        }
        accepting.store(false, ordering: .releasing)
        if first {
            workQueue.sync {
                stopDrainTimer()
                ring.discardAll()
                converter = nil
            }
        }
        if cancelEngine {
            Task.detached { [engine] in await engine.cancel() }
        }
        if DispatchQueue.getSpecific(key: Self.callbackQueueKey) != id {
            callbackQueue.sync {}
        }
    }

    /// `lk_speech_free`: cancels, then zeroes the ring and the scratch buffer. The caller
    /// guarantees no push is in flight.
    func close() {
        cancel()
        workQueue.sync {
            closed = true
            ring.zeroAll()
            scratch.update(repeating: 0, count: scratchFrames)
        }
    }
}
