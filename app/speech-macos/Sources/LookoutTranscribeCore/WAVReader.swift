import Foundation

/// Mono audio read from a WAV file, for replaying the owner's fixtures through the speech
/// library (PRD §10.6 replay; voice fixtures come only from the owner and live outside git under
/// `$LOOKOUT_DATA_DIR/fixtures/voice/`). The reader only reads: it never writes audio anywhere.
public struct WAVAudio: Sendable, Equatable {
    public let sampleRate: Double
    /// Channels in the file; `mono` is their average.
    public let channels: Int
    /// Description of the stored sample format, such as "PCM 16-bit" or "IEEE float 32-bit".
    public let format: String
    public let mono: [Float]
    /// True when the data chunk declared more bytes than the file holds (common for streamed
    /// WAVs); the whole frames that are present were read.
    public let truncated: Bool

    public var frames: Int { mono.count }
    public var seconds: Double { Double(mono.count) / sampleRate }
}

public enum WAVError: Error, Equatable, CustomStringConvertible {
    case notRIFF
    case notWAVE
    case missingFormatChunk
    case missingDataChunk
    case malformedFormatChunk
    case unsupportedFormat(tag: UInt16, bits: UInt16)
    case invalidChannels(UInt16)
    case invalidSampleRate(UInt32)
    case unreadable(String)

    public var description: String {
        switch self {
        case .notRIFF: "not a RIFF file"
        case .notWAVE: "RIFF file is not WAVE"
        case .missingFormatChunk: "no fmt chunk before the data chunk"
        case .missingDataChunk: "no data chunk"
        case .malformedFormatChunk: "fmt chunk is too short"
        case .unsupportedFormat(let tag, let bits): "unsupported sample format (tag \(tag), \(bits) bits)"
        case .invalidChannels(let c): "invalid channel count \(c)"
        case .invalidSampleRate(let r): "invalid sample rate \(r)"
        case .unreadable(let why): "cannot read the file: \(why)"
        }
    }
}

/// A small RIFF/WAVE reader: PCM 8/16/24/32-bit, IEEE float 32/64-bit, and WAVE_FORMAT_EXTENSIBLE
/// with either subformat; any channel count, averaged to mono. Unknown chunks (LIST, fact, ...)
/// are skipped, odd-sized chunks honour the pad byte.
public enum WAVReader {
    public static func read(path: String) throws -> WAVAudio {
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path), options: .mappedIfSafe)
        } catch {
            throw WAVError.unreadable(error.localizedDescription)
        }
        return try parse(data)
    }

    private struct Format {
        let tag: UInt16
        let channels: UInt16
        let sampleRate: UInt32
        let bits: UInt16
        let blockAlign: UInt16
    }

    public static func parse(_ data: Data) throws -> WAVAudio {
        let bytes = [UInt8](data)
        guard bytes.count >= 12, ascii(bytes, 0) == "RIFF" else { throw WAVError.notRIFF }
        guard ascii(bytes, 8) == "WAVE" else { throw WAVError.notWAVE }
        var offset = 12
        var format: Format?
        while offset + 8 <= bytes.count {
            let id = ascii(bytes, offset)
            let size = Int(u32(bytes, offset + 4))
            let body = offset + 8
            if id == "fmt " {
                format = try parseFormat(bytes, at: body, size: min(size, bytes.count - body))
            } else if id == "data" {
                guard let f = format else { throw WAVError.missingFormatChunk }
                let available = bytes.count - body
                let truncated = size > available
                return try decode(bytes, at: body, count: min(size, available), format: f, truncated: truncated)
            }
            offset = body + size + (size & 1)
        }
        throw format == nil ? WAVError.missingFormatChunk : WAVError.missingDataChunk
    }

    private static func parseFormat(_ b: [UInt8], at o: Int, size: Int) throws -> Format {
        guard size >= 16 else { throw WAVError.malformedFormatChunk }
        var tag = u16(b, o)
        let channels = u16(b, o + 2)
        let rate = u32(b, o + 4)
        let blockAlign = u16(b, o + 12)
        let bits = u16(b, o + 14)
        if tag == 0xFFFE {
            // WAVE_FORMAT_EXTENSIBLE: cbSize (2), valid bits (2), channel mask (4), then the
            // subformat GUID, whose first two bytes are the real format tag.
            guard size >= 40 else { throw WAVError.malformedFormatChunk }
            tag = u16(b, o + 24)
        }
        guard channels > 0 else { throw WAVError.invalidChannels(channels) }
        guard rate >= 1_000, rate <= 768_000 else { throw WAVError.invalidSampleRate(rate) }
        return Format(tag: tag, channels: channels, sampleRate: rate, bits: bits, blockAlign: blockAlign)
    }

    private static func decode(_ b: [UInt8], at o: Int, count: Int, format f: Format, truncated: Bool) throws -> WAVAudio {
        let bytesPerSample: Int
        let name: String
        switch (f.tag, f.bits) {
        case (1, 8): (bytesPerSample, name) = (1, "PCM 8-bit")
        case (1, 16): (bytesPerSample, name) = (2, "PCM 16-bit")
        case (1, 24): (bytesPerSample, name) = (3, "PCM 24-bit")
        case (1, 32): (bytesPerSample, name) = (4, "PCM 32-bit")
        case (3, 32): (bytesPerSample, name) = (4, "IEEE float 32-bit")
        case (3, 64): (bytesPerSample, name) = (8, "IEEE float 64-bit")
        default: throw WAVError.unsupportedFormat(tag: f.tag, bits: f.bits)
        }
        let channels = Int(f.channels)
        let frameBytes = max(Int(f.blockAlign), bytesPerSample * channels)
        let frames = count / frameBytes
        var mono = [Float](repeating: 0, count: frames)
        let scale = 1 / Float(channels)
        for i in 0..<frames {
            var sum: Float = 0
            let frameStart = o + i * frameBytes
            for c in 0..<channels {
                sum += sample(b, frameStart + c * bytesPerSample, tag: f.tag, bytes: bytesPerSample)
            }
            mono[i] = sum * scale
        }
        let partialFrame = count % frameBytes != 0
        return WAVAudio(
            sampleRate: Double(f.sampleRate), channels: channels, format: name, mono: mono,
            truncated: truncated || partialFrame)
    }

    @inline(__always)
    private static func sample(_ b: [UInt8], _ p: Int, tag: UInt16, bytes: Int) -> Float {
        switch (tag, bytes) {
        case (1, 1): return (Float(b[p]) - 128) / 128
        case (1, 2): return Float(Int16(bitPattern: u16(b, p))) / 32_768
        case (1, 3):
            let v = Int32(b[p]) | (Int32(b[p + 1]) << 8) | (Int32(Int8(bitPattern: b[p + 2])) << 16)
            return Float(v) / 8_388_608
        case (1, 4): return Float(Double(Int32(bitPattern: u32(b, p))) / 2_147_483_648)
        case (3, 4): return Float(bitPattern: u32(b, p))
        default:
            let lo = UInt64(u32(b, p))
            let hi = UInt64(u32(b, p + 4))
            return Float(Double(bitPattern: lo | (hi << 32)))
        }
    }

    @inline(__always)
    private static func u16(_ b: [UInt8], _ p: Int) -> UInt16 {
        UInt16(b[p]) | (UInt16(b[p + 1]) << 8)
    }

    @inline(__always)
    private static func u32(_ b: [UInt8], _ p: Int) -> UInt32 {
        UInt32(b[p]) | (UInt32(b[p + 1]) << 8) | (UInt32(b[p + 2]) << 16) | (UInt32(b[p + 3]) << 24)
    }

    private static func ascii(_ b: [UInt8], _ p: Int) -> String {
        guard p + 4 <= b.count else { return "" }
        return String(decoding: b[p..<(p + 4)], as: UTF8.self)
    }
}
