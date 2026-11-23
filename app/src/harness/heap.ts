/**
 * A binary min-heap of events ordered by (time, sequence), the order of the Rust key-mash's
 * `BinaryHeap<Reverse<(t, seq, Ev)>>`: earliest first, ties broken by insertion order.
 */
export class EventHeap<E> {
  #t: number[] = [];
  #seq: number[] = [];
  #ev: E[] = [];
  #nextSeq = 0;

  get size(): number {
    return this.#t.length;
  }

  /** Adds `ev` at time `t`; the sequence number is the count of pushes so far, plus one. */
  push(t: number, ev: E): void {
    this.#nextSeq += 1;
    this.#t.push(t);
    this.#seq.push(this.#nextSeq);
    this.#ev.push(ev);
    this.#up(this.#t.length - 1);
  }

  /** Removes and returns the earliest event, or undefined when empty. */
  pop(): { t: number; ev: E } | undefined {
    const n = this.#t.length;
    if (n === 0) return undefined;
    const t = this.#t[0]!;
    const ev = this.#ev[0]!;
    const lastT = this.#t.pop()!;
    const lastSeq = this.#seq.pop()!;
    const lastEv = this.#ev.pop()!;
    if (n > 1) {
      this.#t[0] = lastT;
      this.#seq[0] = lastSeq;
      this.#ev[0] = lastEv;
      this.#down(0);
    }
    return { t, ev };
  }

  #less(i: number, j: number): boolean {
    const ti = this.#t[i]!;
    const tj = this.#t[j]!;
    return ti < tj || (ti === tj && this.#seq[i]! < this.#seq[j]!);
  }

  #swap(i: number, j: number): void {
    const t = this.#t[i]!;
    this.#t[i] = this.#t[j]!;
    this.#t[j] = t;
    const s = this.#seq[i]!;
    this.#seq[i] = this.#seq[j]!;
    this.#seq[j] = s;
    const e = this.#ev[i]!;
    this.#ev[i] = this.#ev[j]!;
    this.#ev[j] = e;
  }

  #up(i: number): void {
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (!this.#less(i, p)) return;
      this.#swap(i, p);
      i = p;
    }
  }

  #down(i: number): void {
    const n = this.#t.length;
    for (;;) {
      const l = 2 * i + 1;
      const r = l + 1;
      let m = i;
      if (l < n && this.#less(l, m)) m = l;
      if (r < n && this.#less(r, m)) m = r;
      if (m === i) return;
      this.#swap(i, m);
      i = m;
    }
  }
}
