// Page side of the M1 1a spike panel (PLAN.md M1 1a checks 3, 4 and 8; PRD §10.4).
// It only paints what Rust sends and reports page-side timestamps back. It never sees audio or
// transcript text, and it reports key names only for the test keys.
(() => {
  'use strict';
  const CELL = 8;
  const bootId = Math.floor(Math.random() * 1e9);
  let lastBeat = performance.now();
  let beats = 0;
  const keysSeen = [];

  function send(ev) {
    try {
      window.__TAURI__.core.invoke('js_event', { ev });
    } catch (e) {
      // No IPC (page opened outside the app): nothing to report to.
    }
  }

  // Heartbeat: when the hidden page is suspended this stops; the gap before the next tick
  // after a reveal shows whether it was suspended, throttled or running.
  setInterval(() => { lastBeat = performance.now(); beats += 1; }, 50);

  const canvas = document.getElementById('tag');
  function drawCells(cells) {
    const dpr = window.devicePixelRatio || 1;
    const w = cells.length * CELL;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(CELL * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(CELL * dpr);
      canvas.style.width = w + 'px';
      canvas.style.height = CELL + 'px';
    }
    const ctx = canvas.getContext('2d', { alpha: false });
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
    for (let i = 0; i < cells.length; i += 1) {
      ctx.fillStyle = cells[i] ? '#ffffff' : '#000000';
      ctx.fillRect(i * CELL, 0, CELL, CELL);
    }
  }

  window.__spike = {
    setCheck(name) { document.getElementById('check').textContent = name; },
    setText(s) { document.getElementById('status').textContent = s; },
    // Draws a tag, then reports the next animation frame (a diagnostic only: rAF runs 1-3
    // vsyncs before WindowServer composites, PRD §10.4).
    setTag(cells, gen, tier, seq) {
      const setTs = performance.now();
      drawCells(cells);
      requestAnimationFrame((rafTs) => send({ kind: 'tag_drawn', gen, tier, seq, set_ts: setTs, raf_ts: rafTs }));
    },
    // Called by Rust right after the panel is shown on key-down. Runs only once the page is
    // awake, so the round trip to Rust times the wake-up.
    reveal(seq, cells, gen, tier) {
      const evalTs = performance.now();
      const beatGap = evalTs - lastBeat;
      drawCells(cells);
      requestAnimationFrame((rafTs) => send({
        kind: 'wake', seq, gen, tier, eval_ts: evalTs, raf_ts: rafTs, beat_gap_ms: beatGap,
        beats, boot_id: bootId, visibility: document.visibilityState,
      }));
    },
  };

  document.addEventListener('visibilitychange', () => {
    const ts = performance.now();
    send({ kind: 'visibility', state: document.visibilityState, ts, beat_gap_ms: ts - lastBeat, boot_id: bootId });
  });

  const TEST_KEYS = new Set(['m', 'M', 'l', 'L', 'c', 'C', 'ArrowLeft', 'ArrowRight', 'Escape']);
  window.addEventListener('keydown', (e) => {
    const key = TEST_KEYS.has(e.key) ? e.key : 'other';
    keysSeen.push(key);
    document.getElementById('keys').textContent = 'keys seen: ' + keysSeen.slice(-8).join(' ');
    send({ kind: 'key', key, has_focus: document.hasFocus(), ts: performance.now() });
  });

  window.addEventListener('focus', () => send({ kind: 'page_focus', focused: true, ts: performance.now() }));
  window.addEventListener('blur', () => send({ kind: 'page_focus', focused: false, ts: performance.now() }));

  send({
    kind: 'boot', boot_id: bootId, time_origin: performance.timeOrigin, ts: performance.now(),
    dpr: window.devicePixelRatio || 1, visibility: document.visibilityState,
  });
})();
