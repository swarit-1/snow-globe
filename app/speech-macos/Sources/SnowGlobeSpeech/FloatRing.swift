import Darwin
import Synchronization

/// A single-producer, single-consumer ring of `Float` samples (PRD §7.1: the capture thread
/// pushes audio while the key is held; the ABI's push must not block an audio thread).
///
/// `write` is the producer side, made for a real-time audio thread: two atomic loads, at most
/// two `memcpy`s and one atomic store. No lock, no allocation, no system call. `read` and
/// `discardAll` are the consumer side and run on one thread at a time (the session's work queue).
///
/// The indices count frames since the ring was created and never wrap in practice (2^63 frames).
/// The consumer zeroes every region it consumes, and `zeroAll` clears the storage, so audio does
/// not linger in RAM after it has been handed on (PRD §15.1: audio is held for the query only).
package final class FloatRing: @unchecked Sendable {
    package let capacity: Int
    private let mask: Int
    private let storage: UnsafeMutablePointer<Float>
    /// Frames written so far; stored by the producer only.
    private let written = Atomic<Int>(0)
    /// Frames consumed so far; stored by the consumer only.
    private let consumed = Atomic<Int>(0)

    /// A ring holding at least `minimumCapacity` frames (rounded up to a power of two).
    package init(minimumCapacity: Int) {
        var c = 1
        while c < max(minimumCapacity, 1) { c <<= 1 }
        capacity = c
        mask = c - 1
        storage = .allocate(capacity: c)
        storage.initialize(repeating: 0, count: c)
    }

    deinit {
        zeroAll()
        storage.deallocate()
    }

    /// Frames written so far (producer progress), from any thread.
    package var totalWritten: Int { written.load(ordering: .acquiring) }

    /// Frames waiting to be read, from any thread.
    package var available: Int { written.load(ordering: .acquiring) - consumed.load(ordering: .acquiring) }

    /// Producer: copies `count` frames in, or nothing at all when they don't fit. Real-time safe.
    @inline(__always)
    package func write(_ source: UnsafePointer<Float>, count: Int) -> Bool {
        let w = written.load(ordering: .relaxed)
        let r = consumed.load(ordering: .acquiring)
        guard count <= capacity - (w - r) else { return false }
        let start = w & mask
        let first = min(count, capacity - start)
        (storage + start).update(from: source, count: first)
        if first < count {
            storage.update(from: source + first, count: count - first)
        }
        written.store(w + count, ordering: .releasing)
        return true
    }

    /// Consumer: moves up to `maxCount` frames into `destination`, zeroes them in the ring and
    /// returns how many it moved.
    package func read(into destination: UnsafeMutablePointer<Float>, maxCount: Int) -> Int {
        let r = consumed.load(ordering: .relaxed)
        let w = written.load(ordering: .acquiring)
        let n = min(w - r, maxCount)
        guard n > 0 else { return 0 }
        let start = r & mask
        let first = min(n, capacity - start)
        destination.update(from: storage + start, count: first)
        (storage + start).update(repeating: 0, count: first)
        if first < n {
            (destination + first).update(from: storage, count: n - first)
            storage.update(repeating: 0, count: n - first)
        }
        consumed.store(r + n, ordering: .releasing)
        return n
    }

    /// Consumer: drops and zeroes everything waiting to be read; returns how many frames it dropped.
    @discardableResult
    package func discardAll() -> Int {
        let r = consumed.load(ordering: .relaxed)
        let w = written.load(ordering: .acquiring)
        let n = w - r
        guard n > 0 else { return 0 }
        let start = r & mask
        let first = min(n, capacity - start)
        (storage + start).update(repeating: 0, count: first)
        if first < n {
            storage.update(repeating: 0, count: n - first)
        }
        consumed.store(r + n, ordering: .releasing)
        return n
    }

    /// Zeroes the whole storage with `memset_s`, which the compiler may not remove. Call it only
    /// when no producer can write (after the last push has returned).
    package func zeroAll() {
        let bytes = capacity * MemoryLayout<Float>.stride
        _ = memset_s(storage, bytes, 0, bytes)
    }

    /// True when every sample in the storage is zero (for the privacy tests).
    package func storageIsAllZero() -> Bool {
        for i in 0..<capacity where storage[i] != 0 { return false }
        return true
    }
}
