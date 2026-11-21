import Foundation

/// Builds the session's transcript from the recognizer's results (PRD §7.1 partials and their
/// stability, §7.2 prefetch on partials).
///
/// Apple's rules (`SpeechModuleResult.isFinal`, SDK 26.2 documentation): a final result is never
/// revised and no later result covers its range; a volatile result may be replaced by a later
/// result over the same audio, and "there is no guarantee that this result will be reissued"
/// as final. `resultsFinalizationTime` says everything before it is final. So:
/// - a new result replaces every volatile segment whose audio range it overlaps;
/// - a volatile segment that ends at or before the new result's finalization time is stale
///   (the final results for that audio have arrived) and is dropped;
/// - final segments accumulate.
///
/// Three views of the same state:
/// - `current`, the hypothesis: the final segments and the volatile tail, in time order (PARTIAL);
/// - `stable`, the part of the hypothesis the recognizer will not revise: the final segments
///   that come before the first volatile one (PRD §7.1 lever 2, "a stable partial"; STABLE).
///   Built from the same ordered list as `current`, so its text is always the start of
///   `current`'s text (a UTF-8 byte prefix). Under Apple's rules no volatile segment precedes a
///   final one, so it is all the final segments;
/// - `finalTranscript`: the final segments only (FINAL). Once the recognizer has finalized
///   through the end of input, a volatile segment that is still left was never confirmed.
///
/// This is Apple's sample pattern (finalized text plus a replaceable volatile tail) with the
/// replacement done by audio range rather than by arrival order. Segments are kept in time order
/// with ties in arrival order, and at equal start times a final segment comes before a volatile
/// one, so every view is deterministic.
package struct TranscriptAssembler: Sendable, Equatable {
    package struct Segment: Sendable, Equatable {
        package let text: String
        package let start: Double
        package let end: Double
    }

    /// Text with the audio range it covers, in seconds since the session's first sample.
    package struct Snapshot: Sendable, Equatable {
        package let text: String
        package let start: Double
        package let end: Double

        package init(text: String, start: Double, end: Double) {
            self.text = text
            self.start = start
            self.end = end
        }

        package static let empty = Snapshot(text: "", start: 0, end: 0)
    }

    package private(set) var finals: [Segment] = []
    package private(set) var volatiles: [Segment] = []

    package init() {}

    package mutating func apply(_ r: RecognizerResult) {
        let start = r.start.isFinite ? r.start : 0
        let end = r.end.isFinite ? max(r.end, start) : start
        let finalizedThrough = r.finalizedThrough.isFinite ? r.finalizedThrough : -Double.infinity
        volatiles.removeAll { v in
            Self.overlaps(v.start, v.end, start, end) || v.end <= finalizedThrough
        }
        let segment = Segment(text: r.text, start: start, end: end)
        if r.isFinal {
            Self.insertInTimeOrder(segment, into: &finals)
        } else {
            Self.insertInTimeOrder(segment, into: &volatiles)
        }
    }

    /// The hypothesis so far: final segments plus the volatile tail.
    package var current: Snapshot {
        Self.snapshot(ordered().map(\.segment))
    }

    /// The stable part of the hypothesis: the final segments before the first volatile one.
    package var stable: Snapshot {
        Self.snapshot(ordered().prefix(while: \.isFinal).map(\.segment))
    }

    /// The finalized transcript.
    package var finalTranscript: Snapshot {
        Self.snapshot(finals.filter { !Self.isBlank($0.text) })
    }

    /// Ranges overlap when they share audio; two empty ranges at the same instant also count.
    package static func overlaps(_ a0: Double, _ a1: Double, _ b0: Double, _ b1: Double) -> Bool {
        (a0 < b1 && b0 < a1) || a0 == b0
    }

    /// The segments with text, in time order; at equal start times finals come first. (Both
    /// lists are already in time order, so this is a merge.)
    private func ordered() -> [(segment: Segment, isFinal: Bool)] {
        var out: [(segment: Segment, isFinal: Bool)] = []
        out.reserveCapacity(finals.count + volatiles.count)
        var i = 0
        var j = 0
        while i < finals.count || j < volatiles.count {
            if j == volatiles.count || (i < finals.count && finals[i].start <= volatiles[j].start) {
                out.append((finals[i], true))
                i += 1
            } else {
                out.append((volatiles[j], false))
                j += 1
            }
        }
        return out.filter { !Self.isBlank($0.segment.text) }
    }

    /// Inserts after every segment that starts at or before `s` (stable for equal starts).
    private static func insertInTimeOrder(_ s: Segment, into list: inout [Segment]) {
        let i = list.lastIndex { $0.start <= s.start }.map { $0 + 1 } ?? 0
        list.insert(s, at: i)
    }

    private static func isBlank(_ s: String) -> Bool {
        s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private static func snapshot(_ used: [Segment]) -> Snapshot {
        guard let first = used.first, let last = used.last else { return .empty }
        return Snapshot(
            text: join(used.map(\.text)), start: first.start, end: used.map(\.end).max() ?? last.end)
    }

    /// Joins segment texts with single spaces. A segment that already starts with whitespace or
    /// with closing punctuation is appended as it is, so either convention for the recognizer's
    /// segment boundaries gives "mount rainier at sunset", never "rainierat" or "rainier , at".
    /// Joining the first k parts always gives a prefix of joining them all (text is only ever
    /// appended, and only trailing whitespace is trimmed), which is what makes `stable` a prefix
    /// of `current`.
    package static func join(_ parts: [String]) -> String {
        var out = ""
        for raw in parts {
            let part = out.isEmpty ? String(raw.drop(while: \.isWhitespace)) : raw
            guard let firstChar = part.first else { continue }
            if out.isEmpty {
                out = part
                continue
            }
            let lastIsSpace = out.last?.isWhitespace ?? true
            if firstChar.isWhitespace {
                out += lastIsSpace ? String(part.drop(while: \.isWhitespace)) : part
            } else if lastIsSpace || noSpaceBefore.contains(firstChar) {
                out += part
            } else {
                out += " " + part
            }
        }
        return out.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let noSpaceBefore: Set<Character> = [",", ".", "!", "?", ";", ":", ")", "]", "}", "'", "’", "…"]
}
