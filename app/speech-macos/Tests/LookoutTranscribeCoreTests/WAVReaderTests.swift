import Foundation
import LookoutTranscribeCore
import XCTest

/// WAV encoding for the tests: generated tones built in memory. Nothing is written to disk.
enum WAVWriter {
    enum Kind { case pcm8, pcm16, pcm24, pcm32, float32, float64 }

    static func make(
        _ channels: [[Float]], rate: UInt32, kind: Kind, extensible: Bool = false, extraChunks: Bool = false,
        declareMoreData: Int = 0
    ) -> Data {
        let (tag, bits): (UInt16, UInt16) = switch kind {
        case .pcm8: (1, 8)
        case .pcm16: (1, 16)
        case .pcm24: (1, 24)
        case .pcm32: (1, 32)
        case .float32: (3, 32)
        case .float64: (3, 64)
        }
        let ch = channels.count
        let frames = channels.first?.count ?? 0
        let bytesPerSample = Int(bits) / 8
        var payload = Data()
        for i in 0..<frames {
            for c in 0..<ch {
                let v = channels[c][i]
                switch kind {
                case .pcm8: payload.append(UInt8(max(0, min(255, Int((v * 128 + 128).rounded())))))
                case .pcm16: le(&payload, UInt16(bitPattern: Int16(max(-32_768, min(32_767, (v * 32_768).rounded())))))
                case .pcm24:
                    let x = Int32(max(-8_388_608, min(8_388_607, (Double(v) * 8_388_608).rounded())))
                    let u = UInt32(bitPattern: x)
                    payload.append(contentsOf: [UInt8(u & 0xFF), UInt8((u >> 8) & 0xFF), UInt8((u >> 16) & 0xFF)])
                case .pcm32:
                    le(&payload, UInt32(bitPattern: Int32(max(-2_147_483_648, min(2_147_483_647, (Double(v) * 2_147_483_648).rounded())))))
                case .float32: le(&payload, v.bitPattern)
                case .float64:
                    let b = Double(v).bitPattern
                    le(&payload, UInt32(b & 0xFFFF_FFFF))
                    le(&payload, UInt32(b >> 32))
                }
            }
        }
        var fmt = Data()
        le(&fmt, extensible ? UInt16(0xFFFE) : tag)
        le(&fmt, UInt16(ch))
        le(&fmt, rate)
        le(&fmt, rate * UInt32(ch * bytesPerSample))
        le(&fmt, UInt16(ch * bytesPerSample))
        le(&fmt, bits)
        if extensible {
            le(&fmt, UInt16(22))
            le(&fmt, bits)
            le(&fmt, UInt32(0))
            le(&fmt, tag)  // the subformat GUID starts with the real tag
            fmt.append(contentsOf: [0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71])
        }
        var body = Data("WAVE".utf8)
        if extraChunks {
            // An odd-sized LIST chunk (with its pad byte) before fmt.
            body.append(Data("LIST".utf8))
            le(&body, UInt32(5))
            body.append(contentsOf: [1, 2, 3, 4, 5, 0])
        }
        body.append(Data("fmt ".utf8))
        le(&body, UInt32(fmt.count))
        body.append(fmt)
        if extraChunks {
            body.append(Data("fact".utf8))
            le(&body, UInt32(4))
            le(&body, UInt32(frames))
        }
        body.append(Data("data".utf8))
        le(&body, UInt32(payload.count + declareMoreData))
        body.append(payload)
        var file = Data("RIFF".utf8)
        le(&file, UInt32(body.count))
        file.append(body)
        return file
    }

    static func le<T: FixedWidthInteger>(_ d: inout Data, _ v: T) {
        withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) }
    }
}

func sine(_ n: Int, rate: Double, hz: Double, amplitude: Float) -> [Float] {
    (0..<n).map { amplitude * Float(sin(2 * Double.pi * hz * Double($0) / rate)) }
}

final class WAVReaderTests: XCTestCase {
    private func check(_ kind: WAVWriter.Kind, tolerance: Float, name: String) throws {
        let x = sine(4_410, rate: 44_100, hz: 441, amplitude: 0.5)
        for extensible in [false, true] {
            let a = try WAVReader.parse(WAVWriter.make([x], rate: 44_100, kind: kind, extensible: extensible))
            XCTAssertEqual(a.sampleRate, 44_100)
            XCTAssertEqual(a.channels, 1)
            XCTAssertEqual(a.frames, 4_410)
            XCTAssertEqual(a.format, name)
            XCTAssertFalse(a.truncated)
            XCTAssertEqual(a.seconds, 0.1, accuracy: 1e-12)
            for i in stride(from: 0, to: x.count, by: 7) {
                XCTAssertEqual(a.mono[i], x[i], accuracy: tolerance, "\(name) sample \(i)")
            }
        }
    }

    func testEverySupportedSampleFormatReadsBackTheTone() throws {
        try check(.pcm8, tolerance: 1.0 / 128, name: "PCM 8-bit")
        try check(.pcm16, tolerance: 1.0 / 32_768, name: "PCM 16-bit")
        try check(.pcm24, tolerance: 1.0 / 8_388_608 * 2, name: "PCM 24-bit")
        try check(.pcm32, tolerance: 1e-6, name: "PCM 32-bit")
        try check(.float32, tolerance: 0, name: "IEEE float 32-bit")
        try check(.float64, tolerance: 1e-7, name: "IEEE float 64-bit")
    }

    func testStereoIsAveragedToMono() throws {
        let l = sine(1_600, rate: 16_000, hz: 400, amplitude: 0.6)
        let r = l.map { -$0 * 0.5 }
        let a = try WAVReader.parse(WAVWriter.make([l, r], rate: 16_000, kind: .pcm16))
        XCTAssertEqual(a.channels, 2)
        XCTAssertEqual(a.frames, 1_600)
        for i in 0..<1_600 { XCTAssertEqual(a.mono[i], (l[i] + r[i]) / 2, accuracy: 1e-4) }
    }

    func testUnknownAndOddSizedChunksAreSkipped() throws {
        let x = sine(800, rate: 8_000, hz: 250, amplitude: 0.3)
        let a = try WAVReader.parse(WAVWriter.make([x], rate: 8_000, kind: .float32, extraChunks: true))
        XCTAssertEqual(a.mono, x)
    }

    func testTruncatedDataReadsTheWholeFramesPresent() throws {
        let x = sine(1_000, rate: 16_000, hz: 300, amplitude: 0.4)
        let a = try WAVReader.parse(WAVWriter.make([x], rate: 16_000, kind: .pcm16, declareMoreData: 10_000))
        XCTAssertTrue(a.truncated)
        XCTAssertEqual(a.frames, 1_000)
        var d = WAVWriter.make([x], rate: 16_000, kind: .pcm16)
        d.removeLast()  // half a frame left at the end
        let b = try WAVReader.parse(d)
        XCTAssertEqual(b.frames, 999)
        XCTAssertTrue(b.truncated)
    }

    func testMalformedFilesAreRejectedWithAReason() {
        XCTAssertThrowsError(try WAVReader.parse(Data("hello".utf8))) { XCTAssertEqual($0 as? WAVError, .notRIFF) }
        var notWave = WAVWriter.make([[0]], rate: 8_000, kind: .pcm16)
        notWave.replaceSubrange(8..<12, with: Data("AVI ".utf8))
        XCTAssertThrowsError(try WAVReader.parse(notWave)) { XCTAssertEqual($0 as? WAVError, .notWAVE) }
        var badTag = WAVWriter.make([[0]], rate: 8_000, kind: .pcm16)
        badTag.replaceSubrange(20..<22, with: Data([0x55, 0x00]))  // MP3 in a WAV
        XCTAssertThrowsError(try WAVReader.parse(badTag)) {
            XCTAssertEqual($0 as? WAVError, .unsupportedFormat(tag: 0x55, bits: 16))
        }
        var noData = Data("RIFF".utf8)
        WAVWriter.le(&noData, UInt32(4))
        noData.append(Data("WAVE".utf8))
        XCTAssertThrowsError(try WAVReader.parse(noData)) { XCTAssertEqual($0 as? WAVError, .missingFormatChunk) }
        var zeroRate = WAVWriter.make([[0]], rate: 8_000, kind: .pcm16)
        zeroRate.replaceSubrange(24..<28, with: Data([0, 0, 0, 0]))
        XCTAssertThrowsError(try WAVReader.parse(zeroRate)) { XCTAssertEqual($0 as? WAVError, .invalidSampleRate(0)) }
        XCTAssertThrowsError(try WAVReader.read(path: "/nonexistent/lookout/none.wav")) {
            guard case .unreadable = $0 as? WAVError else { return XCTFail("\($0)") }
        }
    }
}
