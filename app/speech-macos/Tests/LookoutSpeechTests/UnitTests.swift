@preconcurrency import AVFoundation
import Foundation
import LookoutSpeech
import LookoutSpeechC
import LookoutSpeechMocks
import Speech
import Synchronization
import XCTest

/// The ring buffer between the audio thread and the work queue.
final class FloatRingTests: XCTestCase {
    func testCapacityRoundsUpToAPowerOfTwo() {
        XCTAssertEqual(FloatRing(minimumCapacity: 1).capacity, 1)
        XCTAssertEqual(FloatRing(minimumCapacity: 1_000).capacity, 1_024)
        XCTAssertEqual(FloatRing(minimumCapacity: 960_000).capacity, 1_048_576, "48 kHz x 20 s")
    }

    func testWrapAroundKeepsOrderAndZeroesWhatWasRead() {
        let ring = FloatRing(minimumCapacity: 8)
        var out = [Float](repeating: -1, count: 8)
        var next: Float = 1
        var expect: Float = 1
        for round in 0..<50 {
            let n = 1 + round % 7
            let chunk = (0..<n).map { _ -> Float in defer { next += 1 }; return next }
            XCTAssertTrue(chunk.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: n) })
            let got = out.withUnsafeMutableBufferPointer { ring.read(into: $0.baseAddress!, maxCount: 8) }
            XCTAssertEqual(got, n)
            for k in 0..<got {
                XCTAssertEqual(out[k], expect)
                expect += 1
            }
            XCTAssertTrue(ring.storageIsAllZero(), "consumed samples do not stay in the ring")
        }
    }

    func testAFullRingRefusesTheWholeChunk() {
        let ring = FloatRing(minimumCapacity: 8)
        let six = [Float](repeating: 1, count: 6)
        XCTAssertTrue(six.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: 6) })
        XCTAssertFalse(six.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: 3) }, "6 + 3 > 8")
        XCTAssertEqual(ring.available, 6, "nothing of the refused chunk went in")
        XCTAssertTrue(six.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: 2) })
        XCTAssertEqual(ring.available, 8)
        XCTAssertEqual(ring.discardAll(), 8)
        XCTAssertEqual(ring.available, 0)
        XCTAssertTrue(ring.storageIsAllZero())
        XCTAssertEqual(ring.totalWritten, 8)
    }

    func testOneProducerOneConsumerSeeEverySampleInOrder() {
        let ring = FloatRing(minimumCapacity: 4_096)
        let total = 2_000_000
        let done = DispatchSemaphore(value: 0)
        let errors = Atomic<Int>(0)
        let r = RingBox(ring)
        Thread.detachNewThread {
            var buf = [Float](repeating: 0, count: 256)
            var v = 0
            while v < total {
                let n = min(256, total - v)
                for k in 0..<n { buf[k] = Float(v + k) }
                while !buf.withUnsafeBufferPointer({ r.ring.write($0.baseAddress!, count: n) }) { sched_yield() }
                v += n
            }
            done.signal()
        }
        var expect = 0
        var out = [Float](repeating: 0, count: 1_000)
        while expect < total {
            let n = out.withUnsafeMutableBufferPointer { ring.read(into: $0.baseAddress!, maxCount: 1_000) }
            for k in 0..<n where out[k] != Float(expect + k) { _ = errors.wrappingAdd(1, ordering: .relaxed) }
            expect += n
            if n == 0 { sched_yield() }
        }
        done.wait()
        XCTAssertEqual(errors.load(ordering: .acquiring), 0)
        XCTAssertEqual(expect, total)
    }
}

struct RingBox: @unchecked Sendable {
    let ring: FloatRing
    init(_ r: FloatRing) { ring = r }
}

/// The AVAudioConverter step between the session's rate and the recognizer's format.
final class PCMConverterTests: XCTestCase {
    private func format(_ f: AVAudioCommonFormat, _ rate: Double, channels: AVAudioChannelCount = 1) -> AVAudioFormat {
        AVAudioFormat(commonFormat: f, sampleRate: rate, channels: channels, interleaved: false)!
    }

    private func samples(_ b: AVAudioPCMBuffer) -> [Float] {
        let n = Int(b.frameLength)
        if let i = b.int16ChannelData?[0] { return (0..<n).map { Float(i[$0]) / 32_768 } }
        if let f = b.floatChannelData?[0] { return (0..<n).map { f[$0] } }
        return []
    }

    private func run(_ c: PCMConverter, _ x: [Float], chunk: Int) throws -> [Float] {
        var out: [Float] = []
        try x.withUnsafeBufferPointer { b in
            var o = 0
            while o < b.count {
                let n = min(chunk, b.count - o)
                for buf in try c.convert(b.baseAddress! + o, count: n) { out += samples(buf) }
                o += n
            }
        }
        if let t = try c.flush() { out += samples(t) }
        return out
    }

    func testSameFormatPassesSamplesThrough() throws {
        let c = try PCMConverter(inputSampleRate: 16_000, output: format(.pcmFormatFloat32, 16_000))
        let x = tone(seconds: 0.1, rate: 16_000)
        XCTAssertEqual(try run(c, x, chunk: 160), x)
        XCTAssertNil(try c.flush(), "a second flush is empty")
    }

    func test48kFloatTo16kInt16KeepsTheToneAndTheDuration() throws {
        let c = try PCMConverter(inputSampleRate: 48_000, output: format(.pcmFormatInt16, 16_000))
        let y = try run(c, tone(seconds: 2.0, rate: 48_000, hz: 440, amplitude: 0.5), chunk: 512)
        XCTAssertEqual(Double(y.count), 32_000, accuracy: 32)
        XCTAssertEqual(c.inputFrames, 96_000)
        XCTAssertEqual(c.outputFrames, y.count)
        let mid = y[1_600..<30_400]
        XCTAssertEqual(zeroCrossingHz(mid, rate: 16_000), 440, accuracy: 1)
        XCTAssertEqual(rms(mid), 0.5 / sqrt(2), accuracy: 0.005)
    }

    func test44100To16kAndSilence() throws {
        let c = try PCMConverter(inputSampleRate: 44_100, output: format(.pcmFormatInt16, 16_000))
        let y = try run(c, tone(seconds: 1.0, rate: 44_100, hz: 1_000), chunk: 441)
        XCTAssertEqual(Double(y.count), 16_000, accuracy: 32)
        XCTAssertEqual(zeroCrossingHz(y[1_000..<15_000], rate: 16_000), 1_000, accuracy: 2)
        let d = try PCMConverter(inputSampleRate: 44_100, output: format(.pcmFormatInt16, 16_000))
        var pad: [Float] = []
        XCTAssertTrue(try d.convertSilence(frames: 44_100 / 2) { pad += samples($0) })
        XCTAssertEqual(Double(pad.count + ((try d.flush()).map { Int($0.frameLength) } ?? 0)), 8_000, accuracy: 32)
        XCTAssertTrue(pad.allSatisfy { $0 == 0 })
    }

    func testTheSilencePadStopsWhenAskedBeforeAChunk() throws {
        let c = try PCMConverter(inputSampleRate: 48_000, output: format(.pcmFormatInt16, 16_000))
        var chunks = 0
        let done = try c.convertSilence(frames: 96_000, shouldStop: { chunks >= 3 }) { _ in chunks += 1 }
        XCTAssertFalse(done)
        XCTAssertEqual(c.inputFrames, 3 * PCMConverter.chunkFrames, "three chunks went in, then it stopped")
    }

    /// Glitches in the input (NaN, infinities, samples far outside -1...1) neither fail the
    /// conversion nor spoil the audio after them.
    func testNonFiniteAndOverRangeSamplesConvertWithoutError() throws {
        let c = try PCMConverter(inputSampleRate: 48_000, output: format(.pcmFormatInt16, 16_000))
        var x = tone(seconds: 1.0, rate: 48_000, hz: 440, amplitude: 0.5)
        let glitches: [Float] = [.nan, .infinity, -.infinity, 10, -10, .greatestFiniteMagnitude]
        for i in stride(from: 0, to: 24_000, by: 97) { x[i] = glitches[i % glitches.count] }
        let y = try run(c, x, chunk: 480)
        XCTAssertEqual(Double(y.count), 16_000, accuracy: 40)
        XCTAssertTrue(y.allSatisfy { $0.isFinite && abs($0) <= 1 }, "Int16 output is always in range")
        // The second half (no glitches) is the clean tone again.
        let clean = y[9_000..<15_000]
        XCTAssertEqual(zeroCrossingHz(clean, rate: 16_000), 440, accuracy: 2)
        XCTAssertEqual(rms(clean), 0.5 / sqrt(2), accuracy: 0.01)
    }

    /// Every rate lk_speech_start accepts, 8 to 384 kHz, converts to the recognizer's rate.
    func testEveryAcceptedRateConvertsToTheRecognizersRate() throws {
        let rates: [Double] = [8_000, 11_025, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000, 88_200, 96_000,
                               176_400, 192_000, 352_800, 384_000]
        for rate in rates {
            let c = try PCMConverter(inputSampleRate: rate, output: format(.pcmFormatInt16, 16_000))
            let y = try run(c, tone(seconds: 0.5, rate: rate, hz: 440), chunk: Int(rate / 100))
            XCTAssertEqual(Double(y.count), 8_000, accuracy: 40, "\(rate) Hz")
            XCTAssertEqual(zeroCrossingHz(y[1_000..<7_000], rate: 16_000), 440, accuracy: 2, "\(rate) Hz")
        }
    }

    /// The input copy is zeroed after each conversion (PRD §15.1), and zeroing it changes nothing:
    /// the output is bit-identical to AVAudioConverter's on the same chunks with no zeroing.
    func testZeroingTheInputCopyLeavesTheOutputBitIdentical() throws {
        let rate = 48_000.0
        let out = format(.pcmFormatInt16, 16_000)
        let x = tone(seconds: 1.0, rate: rate, hz: 440)
        let c = try PCMConverter(inputSampleRate: rate, output: out)
        let y = try run(c, x, chunk: 512)
        XCTAssertEqual(c.inputCopiesZeroed, (x.count + 511) / 512, "every input copy was zeroed")
        XCTAssertEqual(y, try referenceConversion(x, rate: rate, to: out, chunk: 512))
        // Same format: the copy is what the recognizer gets, so it is not zeroed.
        let same = try PCMConverter(inputSampleRate: 16_000, output: format(.pcmFormatFloat32, 16_000))
        _ = try run(same, tone(seconds: 0.1, rate: 16_000), chunk: 160)
        XCTAssertEqual(same.inputCopiesZeroed, 0)
    }

    /// AVAudioConverter used directly, the way PCMConverter uses it, with no zeroing.
    private func referenceConversion(_ x: [Float], rate: Double, to out: AVAudioFormat, chunk: Int) throws -> [Float] {
        let inFormat = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1))
        let conv = try XCTUnwrap(AVAudioConverter(from: inFormat, to: out))
        var y: [Float] = []
        var o = 0
        while o < x.count {
            let n = min(chunk, x.count - o)
            let inBuf = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(n)))
            for k in 0..<n { inBuf.floatChannelData![0][k] = x[o + k] }
            inBuf.frameLength = AVAudioFrameCount(n)
            let cap = Int((Double(n) * out.sampleRate / rate).rounded(.up)) + 64
            let outBuf = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: out, frameCapacity: AVAudioFrameCount(cap)))
            let given = GivenOnce(inBuf)
            var error: NSError?
            XCTAssertNotEqual(conv.convert(to: outBuf, error: &error) { _, st in given.next(st) }, .error)
            y += samples(outBuf)
            o += n
        }
        let tail = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: out, frameCapacity: 8_192))
        var error: NSError?
        _ = conv.convert(to: tail, error: &error) { _, st in
            st.pointee = .endOfStream
            return nil
        }
        return y + samples(tail)
    }

    func testOnlyMonoTargetsAreAccepted() {
        XCTAssertThrowsError(try PCMConverter(inputSampleRate: 48_000, output: format(.pcmFormatInt16, 16_000, channels: 2))) {
            XCTAssertEqual(($0 as? EngineError)?.code, LK_SPEECH_ERR_AUDIO_FORMAT)
        }
    }
}

private final class GivenOnce: @unchecked Sendable {
    private var buffer: AVAudioPCMBuffer?
    init(_ b: AVAudioPCMBuffer) { buffer = b }
    func next(_ st: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        guard let b = buffer else {
            st.pointee = .noDataNow
            return nil
        }
        buffer = nil
        st.pointee = .haveData
        return b
    }
}

/// Building the hypothesis and the final transcript from volatile and final results.
final class TranscriptAssemblerTests: XCTestCase {
    private func r(_ t: String, _ a: Double, _ b: Double, final: Bool, through: Double? = nil) -> RecognizerResult {
        RecognizerResult(text: t, start: a, end: b, finalizedThrough: through ?? (final ? b : a), isFinal: final)
    }

    func testVolatileTextIsReplacedThenFinalized() {
        var x = TranscriptAssembler()
        x.apply(r("show", 0, 0.3, final: false))
        x.apply(r("show me", 0, 0.5, final: false))
        XCTAssertEqual(x.current.text, "show me")
        x.apply(r("show me mount", 0, 0.8, final: true))
        x.apply(r("ray", 0.8, 1.0, final: false))
        XCTAssertEqual(x.current, .init(text: "show me mount ray", start: 0, end: 1.0))
        x.apply(r("rainier", 0.8, 1.2, final: false))
        XCTAssertEqual(x.current.text, "show me mount rainier")
        XCTAssertEqual(x.finalTranscript.text, "show me mount", "volatile text is not final")
        x.apply(r("rainier at sunset", 0.8, 1.9, final: true))
        XCTAssertEqual(x.finalTranscript, .init(text: "show me mount rainier at sunset", start: 0, end: 1.9))
        XCTAssertEqual(x.volatiles, [])
    }

    func testAFinalOverAShorterRangeDropsTheOverlappingVolatile() {
        var x = TranscriptAssembler()
        x.apply(r("point reyes at", 0, 1.2, final: false))
        x.apply(r("point reyes", 0, 1.0, final: true))
        XCTAssertEqual(x.current.text, "point reyes", "the old volatile overlapped and is gone")
        x.apply(r("at dawn", 1.0, 1.6, final: false))
        XCTAssertEqual(x.current.text, "point reyes at dawn")
    }

    func testStaleVolatileBeforeTheFinalizationTimeIsDropped() {
        var x = TranscriptAssembler()
        x.apply(r("uh", 0.0, 0.4, final: false))
        // A later final elsewhere says everything before 0.5 s is final: "uh" was never confirmed.
        x.apply(r("yosemite", 0.5, 1.0, final: true, through: 1.0))
        XCTAssertEqual(x.current.text, "yosemite")
        XCTAssertEqual(x.finalTranscript.text, "yosemite")
    }

    func testAnEmptyVolatileWithdrawsTheTail() {
        var x = TranscriptAssembler()
        x.apply(r("grand", 0, 0.5, final: true))
        x.apply(r("cannon", 0.5, 0.9, final: false))
        x.apply(r("", 0.5, 1.0, final: false))
        XCTAssertEqual(x.current.text, "grand")
    }

    func testNonFiniteTimesAreTolerated() {
        var x = TranscriptAssembler()
        x.apply(RecognizerResult(text: "acadia", start: .nan, end: .infinity, finalizedThrough: .nan, isFinal: true))
        XCTAssertEqual(x.finalTranscript, .init(text: "acadia", start: 0, end: 0))
    }

    func testStableIsTheFinalizedStartOfTheHypothesis() {
        var x = TranscriptAssembler()
        x.apply(r("show", 0, 0.3, final: false))
        XCTAssertEqual(x.stable, .empty, "nothing is final yet")
        x.apply(r("show me", 0, 0.4, final: true))
        x.apply(r("mount", 0.4, 0.7, final: false))
        XCTAssertEqual(x.current.text, "show me mount")
        XCTAssertEqual(x.stable, .init(text: "show me", start: 0, end: 0.4))
        XCTAssertEqual(x.finalTranscript, x.stable)
    }

    func testAtEqualStartTimesTheFinalComesFirst() {
        var x = TranscriptAssembler()
        x.apply(r("grand", 0.5, 0.5, final: true))
        x.apply(r("canyon", 0.5, 0.9, final: false))
        XCTAssertEqual(x.current.text, "grand canyon")
        XCTAssertEqual(x.stable.text, "grand")
    }

    /// For any sequence of results at all (in or out of Apple's rules), the stable text is the
    /// start of the hypothesis, byte for byte: they come from one ordered list (header,
    /// LK_EV_STABLE). Seeded, so a failure is reproducible.
    func testStableIsAlwaysAUTF8PrefixOfTheHypothesis() {
        var rng = SplitMix64(seed: 0x5EED_0F_5_7AB1E)
        let pool = ["show", "me", " mount", "rainier ", "", "  ", ",", ".", "'s", "…", "café", "e\u{301}",
                    "\u{301}", "naïve", "🏔️", "Point Reyes", " at", "\n", "tomorrow."]
        for seq in 0..<2_000 {
            var x = TranscriptAssembler()
            for step in 0..<Int.random(in: 1...12, using: &rng) {
                let a = Double(Int.random(in: 0...30, using: &rng)) / 10
                let b = a + Double(Int.random(in: 0...10, using: &rng)) / 10
                let text = (0..<Int.random(in: 1...3, using: &rng)).map { _ in pool.randomElement(using: &rng)! }.joined()
                let through = Double(Int.random(in: 0...40, using: &rng)) / 10
                x.apply(RecognizerResult(text: text, start: a, end: b, finalizedThrough: through, isFinal: Bool.random(using: &rng)))
                XCTAssertTrue(
                    x.current.text.utf8.starts(with: x.stable.text.utf8),
                    "sequence \(seq) step \(step): '\(x.stable.text)' is not the start of '\(x.current.text)'")
            }
        }
    }

    /// Under Apple's rules (finals never revised, in time order; volatile results after the
    /// finalization time), the stable text is all the final text and it only grows.
    func testUnderApplesRulesStableIsEveryFinalAndOnlyGrows() {
        var rng = SplitMix64(seed: 42)
        let words = ["show", "me", "mount", "rainier", "at", "sunset", "tomorrow", "Yosémite", "🏔️", ","]
        for seq in 0..<500 {
            var x = TranscriptAssembler()
            var finalizedTime = 0.0
            var previous = ""
            for _ in 0..<Int.random(in: 1...15, using: &rng) {
                let d = Double(Int.random(in: 1...6, using: &rng)) / 10
                let text = (0..<Int.random(in: 1...3, using: &rng)).map { _ in words.randomElement(using: &rng)! }.joined(separator: " ")
                if Bool.random(using: &rng) {
                    x.apply(RecognizerResult(text: text, start: finalizedTime, end: finalizedTime + d, finalizedThrough: finalizedTime, isFinal: false))
                } else {
                    x.apply(RecognizerResult(text: text, start: finalizedTime, end: finalizedTime + d, finalizedThrough: finalizedTime + d, isFinal: true))
                    finalizedTime += d
                }
                XCTAssertEqual(x.stable, x.finalTranscript, "sequence \(seq)")
                XCTAssertTrue(x.stable.text.utf8.starts(with: previous.utf8), "sequence \(seq): stable shrank")
                XCTAssertTrue(x.current.text.utf8.starts(with: previous.utf8), "sequence \(seq)")
                previous = x.stable.text
            }
        }
    }

    func testJoinSpacing() {
        XCTAssertEqual(TranscriptAssembler.join(["Mount", "Rainier"]), "Mount Rainier")
        XCTAssertEqual(TranscriptAssembler.join(["Mount", " Rainier"]), "Mount Rainier")
        XCTAssertEqual(TranscriptAssembler.join(["Mount ", " Rainier"]), "Mount Rainier")
        XCTAssertEqual(TranscriptAssembler.join([" Rainier", ", tomorrow", "."]), "Rainier, tomorrow.")
        XCTAssertEqual(TranscriptAssembler.join(["Rainier", "'s summit"]), "Rainier's summit")
        XCTAssertEqual(TranscriptAssembler.join(["", "  ", "Big Bend"]), "Big Bend")
        XCTAssertEqual(TranscriptAssembler.join([]), "")
    }
}

/// Mapping framework errors to the ABI's codes, and the reserve-and-retry step.
final class ErrorMappingTests: XCTestCase {
    func testSpeechErrorsMapToABICodes() {
        let cases: [(SFSpeechError.Code, Int32)] = [
            (.noModel, LK_SPEECH_ASSET_NOT_INSTALLED),
            (.cannotAllocateUnsupportedLocale, LK_SPEECH_LOCALE_UNSUPPORTED),
            (.assetLocaleNotAllocated, LK_SPEECH_ERR_ASSET_RESERVATION),
            (.tooManyAssetLocalesAllocated, LK_SPEECH_ERR_ASSET_RESERVATION),
            (.insufficientResources, LK_SPEECH_ERR_RESOURCES),
            (.audioDisordered, LK_SPEECH_ERR_AUDIO_FORMAT),
            (.unexpectedAudioFormat, LK_SPEECH_ERR_AUDIO_FORMAT),
            (.incompatibleAudioFormats, LK_SPEECH_ERR_AUDIO_FORMAT),
            (.timeout, LK_SPEECH_ERR_TIMEOUT),
            (.internalServiceError, LK_SPEECH_ERR_ENGINE),
            (.moduleOutputFailed, LK_SPEECH_ERR_ENGINE),
        ]
        for (code, want) in cases {
            let e = ErrorMapping.map(SFSpeechError(code))
            XCTAssertEqual(e.code, want, "\(code)")
            XCTAssertTrue(e.detail.hasPrefix(SFSpeechError.errorDomain), e.detail)
        }
        XCTAssertEqual(ErrorMapping.map(CancellationError()).code, LK_SPEECH_ERR_ENGINE)
        XCTAssertEqual(ErrorMapping.map(EngineError(LK_SPEECH_ERR_STATE, "x")), EngineError(LK_SPEECH_ERR_STATE, "x"))
        XCTAssertEqual(ErrorMapping.map(NSError(domain: "other", code: 7)).code, LK_SPEECH_ERR_ENGINE)
        XCTAssertTrue(ErrorMapping.isUnreservedLocale(SFSpeechError(.assetLocaleNotAllocated)))
        XCTAssertFalse(ErrorMapping.isUnreservedLocale(SFSpeechError(.noModel)))
    }

    private struct Unreserved: Error {}
    private struct Other: Error {}

    func testReserveThenRetryOnce() async throws {
        let reserves = Atomic<Int>(0)
        let calls = Atomic<Int>(0)
        let v = try await AssetReservation.retryingOnce(
            isUnreserved: { $0 is Unreserved },
            reserve: { _ = reserves.wrappingAdd(1, ordering: .relaxed) },
            {
                if calls.wrappingAdd(1, ordering: .relaxed).oldValue == 0 { throw Unreserved() }
                return 42
            })
        XCTAssertEqual(v, 42)
        XCTAssertEqual(reserves.load(ordering: .relaxed), 1)
        XCTAssertEqual(calls.load(ordering: .relaxed), 2)
    }

    func testOtherErrorsAreNotRetriedAndASecondRefusalPropagates() async {
        let reserves = Atomic<Int>(0)
        do {
            _ = try await AssetReservation.retryingOnce(
                isUnreserved: { $0 is Unreserved }, reserve: { _ = reserves.wrappingAdd(1, ordering: .relaxed) },
                { () async throws -> Int in throw Other() })
            XCTFail("should throw")
        } catch {
            XCTAssertTrue(error is Other)
        }
        XCTAssertEqual(reserves.load(ordering: .relaxed), 0)
        do {
            _ = try await AssetReservation.retryingOnce(
                isUnreserved: { $0 is Unreserved }, reserve: {}, { () async throws -> Int in throw Unreserved() })
            XCTFail("should throw")
        } catch {
            XCTAssertTrue(error is Unreserved, "retried once only")
        }
    }

    func testAFailedReservationIsAReservationError() async {
        do {
            _ = try await AssetReservation.retryingOnce(
                isUnreserved: { $0 is Unreserved }, reserve: { throw Other() },
                { () async throws -> Int in throw Unreserved() })
            XCTFail("should throw")
        } catch {
            XCTAssertEqual((error as? EngineError)?.code, LK_SPEECH_ERR_ASSET_RESERVATION)
        }
    }

    func testAuthorizationPolicy() {
        XCTAssertFalse(SpeechAuthorization.notDetermined.blocks, "SpeechAnalyzer modules need no authorization")
        XCTAssertFalse(SpeechAuthorization.authorized.blocks)
        XCTAssertTrue(SpeechAuthorization.denied.blocks, "a user's no is honoured")
        XCTAssertTrue(SpeechAuthorization.restricted.blocks)
        XCTAssertTrue(SpeechAuthorization.unknown.blocks)
    }

    func testRetentionMapsToTheFramework() {
        XCTAssertEqual(TranscriberSetup.presetName, "progressiveShortDictation")
    }
}

/// A small seeded generator, so randomized tests are reproducible.
struct SplitMix64: RandomNumberGenerator {
    private var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
}
