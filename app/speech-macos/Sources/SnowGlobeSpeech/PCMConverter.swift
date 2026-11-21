@preconcurrency import AVFoundation
import Darwin
import SnowGlobeSpeechC

/// Converts the session's mono Float32 samples to the PCM format the recognizer asked for
/// (PRD §7.1). SpeechAnalyzer does not resample its input (`bestAvailableAudioFormat` names the
/// format it wants), and SDK 26.2 has no `AnalyzerInputConverter` (that class is macOS 27+), so
/// the conversion uses `AVAudioConverter`, whose resampler keeps its state between calls.
///
/// Used from one thread at a time (the session's work queue). Every call returns new buffers,
/// because the recognizer keeps the buffers it is given.
///
/// Privacy (PRD §15.1): when a conversion takes place, the Float32 copy of the input is zeroed
/// as soon as the converter has consumed it. The SDK's rule is that input data "must not be
/// cleared or re-filled until the input block is called again or the conversion has finished"
/// (AVAudioConverter.h), so `convert` keeps converting until the converter asks for more input,
/// and only then zeroes the copy. When no conversion is needed the copy itself is what the
/// recognizer is given, so it is not zeroed.
package final class PCMConverter {
    package let input: AVAudioFormat
    package let output: AVAudioFormat
    private let converter: AVAudioConverter?
    private var flushed = false
    package private(set) var inputFrames = 0
    package private(set) var outputFrames = 0
    /// Input copies zeroed after conversion (for the privacy tests).
    package private(set) var inputCopiesZeroed = 0

    /// Samples per silence chunk, and the most a drain hands over at once (Session's scratch).
    package static let chunkFrames = 4_096

    /// - Throws: `EngineError` (`SG_SPEECH_ERR_AUDIO_FORMAT`) when the rate or the target format
    ///   is unusable, or when no converter exists between the two formats.
    package init(inputSampleRate: Double, output: AVAudioFormat) throws {
        guard let input = AVAudioFormat(standardFormatWithSampleRate: inputSampleRate, channels: 1)
        else {
            throw EngineError(SG_SPEECH_ERR_AUDIO_FORMAT, "no Float32 format at \(inputSampleRate) Hz")
        }
        guard output.channelCount == 1, output.sampleRate > 0 else {
            throw EngineError(
                SG_SPEECH_ERR_AUDIO_FORMAT,
                "the recognizer asked for \(output.channelCount) channels at \(output.sampleRate) Hz; only mono is supported")
        }
        self.input = input
        self.output = output
        if input == output {
            converter = nil
        } else {
            guard let c = AVAudioConverter(from: input, to: output) else {
                throw EngineError(
                    SG_SPEECH_ERR_AUDIO_FORMAT,
                    "no converter from \(Self.describe(input)) to \(Self.describe(output))")
            }
            converter = c
        }
    }

    /// Converts `count` samples. Returns the converted buffers, usually one; none when the
    /// resampler is still holding the frames back (it keeps a few frames of history until
    /// `flush`).
    package func convert(_ samples: UnsafePointer<Float>, count: Int) throws -> [AVAudioPCMBuffer] {
        guard count > 0, !flushed else { return [] }
        let inBuffer = try makeBuffer(input, frames: count)
        guard let copy = inBuffer.floatChannelData?[0] else {
            throw EngineError(SG_SPEECH_ERR_INTERNAL, "no float channel data")
        }
        copy.update(from: samples, count: count)
        inBuffer.frameLength = AVAudioFrameCount(count)
        inputFrames += count
        guard let converter else {
            outputFrames += count
            return [inBuffer]
        }
        let source = OneShotSource(inBuffer)
        var out: [AVAudioPCMBuffer] = []
        // One output buffer normally takes it all; a second round is a safety net, so that the
        // input copy is consumed (and can be zeroed) whatever the resampler's latency is.
        for _ in 0..<64 {
            let buffer = try makeBuffer(output, frames: outputCapacity(for: count))
            var error: NSError?
            let status = converter.convert(to: buffer, error: &error) { _, inputStatus in
                source.next(inputStatus)
            }
            if status == .error {
                throw EngineError(
                    SG_SPEECH_ERR_AUDIO_FORMAT, "conversion failed: \(error?.localizedDescription ?? "unknown error")")
            }
            if buffer.frameLength > 0 {
                outputFrames += Int(buffer.frameLength)
                out.append(buffer)
            }
            if source.askedAgain { break }
        }
        if source.askedAgain {
            let bytes = count * MemoryLayout<Float>.stride
            _ = memset_s(copy, bytes, 0, bytes)
            inputCopiesZeroed += 1
        }
        return out
    }

    /// Converts a burst of `frames` zero samples (the silence pad, PRD §7.1 lever 1), one chunk at
    /// a time, handing each chunk's output to `deliver`. Stops early, returning false, when
    /// `shouldStop` says so before a chunk (a cancel landed); returns true when it finished.
    package func convertSilence(
        frames: Int, shouldStop: () -> Bool = { false }, deliver: (AVAudioPCMBuffer) -> Void
    ) throws -> Bool {
        guard frames > 0 else { return true }
        let chunk = Self.chunkFrames
        let zeros = UnsafeMutablePointer<Float>.allocate(capacity: chunk)
        zeros.initialize(repeating: 0, count: chunk)
        defer { zeros.deallocate() }
        var left = frames
        while left > 0 {
            if shouldStop() { return false }
            let n = min(left, chunk)
            for b in try convert(zeros, count: n) { deliver(b) }
            left -= n
        }
        return true
    }

    /// Ends the input: returns the frames the resampler still holds. Later calls return nil.
    package func flush() throws -> AVAudioPCMBuffer? {
        guard !flushed else { return nil }
        flushed = true
        guard let converter else { return nil }
        let out = try makeBuffer(output, frames: 8_192)
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            inputStatus.pointee = .endOfStream
            return nil
        }
        if status == .error {
            throw EngineError(
                SG_SPEECH_ERR_AUDIO_FORMAT, "flush failed: \(error?.localizedDescription ?? "unknown error")")
        }
        outputFrames += Int(out.frameLength)
        return out.frameLength > 0 ? out : nil
    }

    private func outputCapacity(for inputCount: Int) -> Int {
        Int((Double(inputCount) * output.sampleRate / input.sampleRate).rounded(.up)) + 64
    }

    private func makeBuffer(_ format: AVAudioFormat, frames: Int) throws -> AVAudioPCMBuffer {
        guard let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
            throw EngineError(SG_SPEECH_ERR_AUDIO_FORMAT, "cannot allocate a \(frames)-frame buffer")
        }
        return b
    }

    package static func describe(_ f: AVAudioFormat) -> String {
        let kind: String
        switch f.commonFormat {
        case .pcmFormatFloat32: kind = "Float32"
        case .pcmFormatFloat64: kind = "Float64"
        case .pcmFormatInt16: kind = "Int16"
        case .pcmFormatInt32: kind = "Int32"
        default: kind = "format \(f.commonFormat.rawValue)"
        }
        return "\(kind) \(f.channelCount) ch \(f.sampleRate) Hz\(f.isInterleaved ? " interleaved" : "")"
    }
}

/// Hands one buffer to `AVAudioConverter`'s input block, then reports "no data now", so the
/// converter keeps its resampler state for the next call instead of ending the stream. Asked
/// again after the buffer went out, it records that the converter has consumed the buffer.
///
/// The SDK marks the input block `@Sendable`, but `convert(to:error:withInputFrom:)` calls it
/// synchronously on the calling thread, before it returns; the source never leaves that call.
private final class OneShotSource: @unchecked Sendable {
    private var buffer: AVAudioPCMBuffer?
    private(set) var askedAgain = false

    init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }

    func next(_ status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        if let b = buffer {
            buffer = nil
            status.pointee = .haveData
            return b
        }
        askedAgain = true
        status.pointee = .noDataNow
        return nil
    }
}
