//! Randomised key-mash stress test (PRD §10.3, §13.1 "Stale paint: ≥ 10,000 randomised cycles,
//! 0 stale paints").
//!
//! A discrete-event simulation on a simulated clock (microseconds) drives the real
//! `GenCounter`, `CancelToken` and `PaintGate`. Each cycle is a key press: hold, release, speech
//! final, parse, then Tier 0/1/2 stages with injected random delays (heavy-tailed for speech and
//! Tier 2). Presses often come faster than the pipeline (mashing), so results of older generations
//! arrive late and out of order. Stages that see their token cancelled drop their work only 60 %
//! of the time; the rest deliver late results anyway, to stress the gate. Users scrub (new scene
//! for the same generation) and press Esc, which goes through `lifecycle::escape` (cancel the
//! token and dismiss the gate). A result is composited at the next vsync (plus 0–2 frames of
//! webview delay), where the gate is checked immediately before the paint.
//!
//! An oracle keeps, from the simulation's own events, what the user should be seeing, and states
//! the PRD §10.3 rule from that: a paint is fresh if it was made for the latest key press, Esc has
//! not been pressed since, it shows the scene selected now, and its tier is higher than any shown
//! for that scene. The real gate must make **zero stale paints** and must never drop a fresh
//! result. Four deliberately broken gates run on the same streams (generation check only, Esc
//! that only cancels the token, no tier order, no scene check) must each be caught making stale
//! paints, which shows the oracle sees every kind.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap, HashMap, HashSet};

use lookout_core::lifecycle::{
    CancelToken, GenCounter, PaintDecision, PaintGate, PaintKey, QueryGen, SceneHash, StaleReason,
    Tier, escape,
};

const VSYNC_US: u64 = 16_667;
const CYCLES_PER_SEED: u64 = 10_000;
const SEEDS: [u64; 4] = [0x00C0_FFEE, 0x5EED_0002, 0xBADC_0DE5, 0x0DDB_A115];

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn range(&mut self, lo: u64, hi: u64) -> u64 {
        lo + self.next() % (hi - lo)
    }
    fn chance(&mut self, p: f64) -> bool {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64 <= p
    }
    /// Mostly `lo..hi`, with a 5 % tail up to `tail`.
    fn heavy(&mut self, lo: u64, hi: u64, tail: u64) -> u64 {
        if self.chance(0.05) {
            self.range(hi, tail)
        } else {
            self.range(lo, hi)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Ev {
    KeyDown,
    Esc,
    KeyUp {
        generation: u64,
    },
    SttFinal {
        generation: u64,
    },
    Parsed {
        generation: u64,
        scene: u64,
    },
    Scrub {
        generation: u64,
    },
    StageDone {
        generation: u64,
        scene: u64,
        tier: Tier,
    },
    Composite {
        generation: u64,
        scene: u64,
        tier: Tier,
    },
}

trait Gate {
    fn begin(&mut self, generation: QueryGen);
    fn set_scene(&mut self, generation: QueryGen, scene: SceneHash);
    /// Esc for the query whose token is `token`.
    fn escape(&mut self, token: &CancelToken);
    fn try_paint(&mut self, key: &PaintKey) -> PaintDecision;
}

impl Gate for PaintGate {
    fn begin(&mut self, generation: QueryGen) {
        PaintGate::begin(self, generation);
    }
    fn set_scene(&mut self, generation: QueryGen, scene: SceneHash) {
        PaintGate::set_scene(self, generation, scene);
    }
    fn escape(&mut self, token: &CancelToken) {
        escape(token, self);
    }
    fn try_paint(&mut self, key: &PaintKey) -> PaintDecision {
        PaintGate::try_paint(self, key)
    }
}

/// Negative control: checks the generation only (no scene, no tier order, Esc ignored).
struct GenerationOnlyGate(QueryGen);

impl Gate for GenerationOnlyGate {
    fn begin(&mut self, generation: QueryGen) {
        self.0 = generation;
    }
    fn set_scene(&mut self, _: QueryGen, _: SceneHash) {}
    fn escape(&mut self, token: &CancelToken) {
        token.cancel();
    }
    fn try_paint(&mut self, key: &PaintKey) -> PaintDecision {
        if key.generation == self.0 {
            PaintDecision::Accept
        } else {
            PaintDecision::DropStale(StaleReason::Generation)
        }
    }
}

/// Negative controls: the real gate with one rule broken.
#[derive(Debug, Clone, Copy)]
enum Fault {
    /// Esc only cancels the token (the gate before the fix of 2026-11-19).
    EscCancelsTokenOnly,
    /// Accepts a tier at or below the one on screen.
    NoTierOrder,
    /// Accepts a result for another scene of the current generation.
    NoSceneCheck,
}

struct FaultyGate(PaintGate, Fault);

impl Gate for FaultyGate {
    fn begin(&mut self, generation: QueryGen) {
        self.0.begin(generation);
    }
    fn set_scene(&mut self, generation: QueryGen, scene: SceneHash) {
        self.0.set_scene(generation, scene);
    }
    fn escape(&mut self, token: &CancelToken) {
        match self.1 {
            Fault::EscCancelsTokenOnly => token.cancel(),
            _ => escape(token, &mut self.0),
        }
    }
    fn try_paint(&mut self, key: &PaintKey) -> PaintDecision {
        match (self.0.try_paint(key), self.1) {
            (PaintDecision::DropStale(StaleReason::Tier), Fault::NoTierOrder)
            | (PaintDecision::DropStale(StaleReason::Scene), Fault::NoSceneCheck) => {
                PaintDecision::Accept
            }
            (d, _) => d,
        }
    }
}

#[derive(Debug, Default)]
struct Report {
    presses: u64,
    escapes: u64,
    events: u64,
    composites: u64,
    accepted: u64,
    stale_paints: u64,
    wrong_drops: u64,
    dropped: BTreeMap<String, u64>,
    cancelled_stages: u64,
    late_results: u64,
    scrubs: u64,
    generations_painted: u64,
    sim_seconds: f64,
}

/// What the user should be seeing, kept from the simulation's own events (never from the gate).
struct Oracle {
    /// Generation handed out for the latest key press.
    press: u64,
    /// Esc was pressed after it.
    dismissed: bool,
    /// The scene selected for it: the parse result, then each scrub.
    scene: Option<u64>,
    /// The best tier composited for that scene so far.
    shown: Option<Tier>,
}

impl Oracle {
    fn new(press: u64) -> Oracle {
        Oracle {
            press,
            dismissed: false,
            scene: None,
            shown: None,
        }
    }

    /// PRD §10.3 from the user's side: made for the latest key press, no Esc since, the scene
    /// selected now, and a tier higher than any shown for that scene.
    fn fresh(&self, generation: u64, scene: u64, tier: Tier) -> bool {
        generation == self.press
            && !self.dismissed
            && self.scene == Some(scene)
            && self.shown.is_none_or(|shown| tier > shown)
    }
}

fn simulate<G: Gate>(seed: u64, cycles: u64, gate: &mut G) -> Report {
    let mut rng = Rng(seed);
    let counter = GenCounter::new();
    let mut tokens: HashMap<u64, CancelToken> = HashMap::new();
    let mut queue: BinaryHeap<Reverse<(u64, u64, Ev)>> = BinaryHeap::new();
    let mut seq = 0u64;
    let mut push = |queue: &mut BinaryHeap<Reverse<(u64, u64, Ev)>>, t: u64, ev: Ev| {
        seq += 1;
        queue.push(Reverse((t, seq, ev)));
    };
    let mut oracle = Oracle::new(0);
    let mut report = Report::default();
    let mut painted: HashSet<u64> = HashSet::new();
    let mut last_t = 0;
    push(&mut queue, 0, Ev::KeyDown);
    while let Some(Reverse((t, _, ev))) = queue.pop() {
        report.events += 1;
        last_t = t;
        let cancelled = |tokens: &HashMap<u64, CancelToken>, g: u64| {
            tokens.get(&g).is_some_and(CancelToken::is_cancelled)
        };
        match ev {
            Ev::KeyDown => {
                report.presses += 1;
                let (g, token) = counter.begin();
                tokens.insert(g.0, token);
                gate.begin(g);
                oracle = Oracle::new(g.0);
                let mashing = rng.chance(0.5);
                let hold = if mashing {
                    rng.range(20_000, 150_000)
                } else {
                    rng.range(300_000, 2_000_000)
                };
                push(&mut queue, t + hold, Ev::KeyUp { generation: g.0 });
                if report.presses < cycles {
                    let gap = if mashing {
                        rng.range(5_000, 250_000)
                    } else {
                        rng.range(250_000, 5_000_000)
                    };
                    push(&mut queue, t + gap, Ev::KeyDown);
                }
                if rng.chance(0.05) {
                    push(&mut queue, t + rng.range(50_000, 3_000_000), Ev::Esc);
                }
            }
            Ev::Esc => {
                // Esc (PRD §5.2): the overlay goes idle until the next key press.
                report.escapes += 1;
                if let Some(token) = tokens.get(&oracle.press) {
                    gate.escape(token);
                }
                oracle.dismissed = true;
            }
            Ev::KeyUp { generation } => {
                if cancelled(&tokens, generation) && rng.chance(0.6) {
                    report.cancelled_stages += 1;
                    continue;
                }
                push(
                    &mut queue,
                    t + rng.heavy(60_000, 450_000, 3_000_000),
                    Ev::SttFinal { generation },
                );
            }
            Ev::SttFinal { generation } => {
                if cancelled(&tokens, generation) && rng.chance(0.6) {
                    report.cancelled_stages += 1;
                    continue;
                }
                let scene = rng.next();
                push(
                    &mut queue,
                    t + rng.range(100, 5_000),
                    Ev::Parsed { generation, scene },
                );
            }
            Ev::Parsed { generation, scene } => {
                gate.set_scene(QueryGen(generation), SceneHash(scene));
                if generation == oracle.press && !oracle.dismissed && oracle.scene != Some(scene) {
                    oracle.scene = Some(scene);
                    oracle.shown = None;
                }
                push(
                    &mut queue,
                    t + rng.range(2_000, 90_000),
                    Ev::StageDone {
                        generation,
                        scene,
                        tier: Tier::Tier0,
                    },
                );
                push(
                    &mut queue,
                    t + rng.heavy(10_000, 400_000, 1_500_000),
                    Ev::StageDone {
                        generation,
                        scene,
                        tier: Tier::Tier1,
                    },
                );
                if rng.chance(0.85) {
                    push(
                        &mut queue,
                        t + rng.heavy(500_000, 3_000_000, 9_000_000),
                        Ev::StageDone {
                            generation,
                            scene,
                            tier: Tier::Tier2,
                        },
                    );
                }
                if rng.chance(0.35) {
                    push(
                        &mut queue,
                        t + rng.range(30_000, 1_500_000),
                        Ev::Scrub { generation },
                    );
                }
            }
            Ev::Scrub { generation } => {
                if generation != oracle.press || oracle.dismissed || oracle.scene.is_none() {
                    continue; // only the query on screen can be scrubbed
                }
                report.scrubs += 1;
                let scene = rng.next();
                gate.set_scene(QueryGen(generation), SceneHash(scene));
                oracle.scene = Some(scene);
                oracle.shown = None;
                push(
                    &mut queue,
                    t + rng.range(1_000, 30_000),
                    Ev::StageDone {
                        generation,
                        scene,
                        tier: Tier::Tier0,
                    },
                );
                push(
                    &mut queue,
                    t + rng.range(3_000, 60_000),
                    Ev::StageDone {
                        generation,
                        scene,
                        tier: Tier::Tier1,
                    },
                );
                if rng.chance(0.3) {
                    push(
                        &mut queue,
                        t + rng.range(400_000, 3_000_000),
                        Ev::StageDone {
                            generation,
                            scene,
                            tier: Tier::Tier2,
                        },
                    );
                }
                if rng.chance(0.4) {
                    push(
                        &mut queue,
                        t + rng.range(10_000, 500_000),
                        Ev::Scrub { generation },
                    );
                }
            }
            Ev::StageDone {
                generation,
                scene,
                tier,
            } => {
                if cancelled(&tokens, generation) {
                    if rng.chance(0.6) {
                        report.cancelled_stages += 1;
                        continue;
                    }
                    report.late_results += 1;
                }
                let next_vsync = (t / VSYNC_US + 1 + rng.range(0, 3)) * VSYNC_US;
                push(
                    &mut queue,
                    next_vsync,
                    Ev::Composite {
                        generation,
                        scene,
                        tier,
                    },
                );
            }
            Ev::Composite {
                generation,
                scene,
                tier,
            } => {
                report.composites += 1;
                let key = PaintKey {
                    generation: QueryGen(generation),
                    scene: SceneHash(scene),
                    tier,
                };
                let fresh = oracle.fresh(generation, scene, tier);
                match gate.try_paint(&key) {
                    PaintDecision::Accept => {
                        report.accepted += 1;
                        painted.insert(generation);
                        if fresh {
                            oracle.shown = Some(tier);
                        } else {
                            report.stale_paints += 1;
                        }
                    }
                    PaintDecision::DropStale(reason) => {
                        *report.dropped.entry(format!("{reason:?}")).or_default() += 1;
                        if fresh {
                            report.wrong_drops += 1;
                        }
                    }
                }
            }
        }
    }
    report.generations_painted = painted.len() as u64;
    report.sim_seconds = last_t as f64 / 1e6;
    report
}

#[test]
fn key_mash_makes_zero_stale_paints() {
    let mut totals = Report::default();
    for seed in SEEDS {
        let r = simulate(seed, CYCLES_PER_SEED, &mut PaintGate::new());
        println!(
            "seed {seed:#010x}: {} presses, {} Esc, {} events, {} composites, {} painted, {} stale, {} fresh dropped, \
             dropped {:?}, {} cancelled stages, {} late results, {} scrubs, {} generations painted, {:.0} s simulated",
            r.presses,
            r.escapes,
            r.events,
            r.composites,
            r.accepted,
            r.stale_paints,
            r.wrong_drops,
            r.dropped,
            r.cancelled_stages,
            r.late_results,
            r.scrubs,
            r.generations_painted,
            r.sim_seconds
        );
        assert!(r.presses >= CYCLES_PER_SEED);
        assert_eq!(r.stale_paints, 0, "seed {seed:#x}: stale paints");
        assert_eq!(r.wrong_drops, 0, "seed {seed:#x}: fresh results dropped");
        // Esc follows 5 % of presses, so its drop path sees fewer late results than the others.
        for (reason, at_least) in [
            ("Generation", 100),
            ("Scene", 100),
            ("Tier", 100),
            ("Dismissed", 50),
        ] {
            assert!(
                r.dropped.get(reason).copied().unwrap_or(0) > at_least,
                "seed {seed:#x}: drop path {reason} barely exercised"
            );
        }
        assert!(
            r.late_results > 1_000,
            "seed {seed:#x}: too few late results to stress the gate"
        );
        assert!(
            r.accepted > r.presses / 2,
            "seed {seed:#x}: the gate accepted too little"
        );
        totals.presses += r.presses;
        totals.accepted += r.accepted;
        totals.stale_paints += r.stale_paints;
        totals.late_results += r.late_results;
    }
    println!(
        "total: {} cycles, {} paints, {} stale paints, {} late results offered to the gate",
        totals.presses, totals.accepted, totals.stale_paints, totals.late_results
    );
}

#[test]
fn a_generation_only_gate_is_caught() {
    for seed in SEEDS {
        let r = simulate(seed, CYCLES_PER_SEED, &mut GenerationOnlyGate(QueryGen(0)));
        println!(
            "negative control (generation only) seed {seed:#010x}: {} stale paints out of {} paints",
            r.stale_paints, r.accepted
        );
        assert!(
            r.stale_paints > 100,
            "the oracle must catch a gate that ignores scene, tier and Esc"
        );
    }
}

#[test]
fn each_broken_rule_is_caught() {
    for fault in [
        Fault::EscCancelsTokenOnly,
        Fault::NoTierOrder,
        Fault::NoSceneCheck,
    ] {
        for seed in SEEDS {
            let r = simulate(
                seed,
                CYCLES_PER_SEED,
                &mut FaultyGate(PaintGate::new(), fault),
            );
            println!(
                "negative control ({fault:?}) seed {seed:#010x}: {} stale paints out of {} paints",
                r.stale_paints, r.accepted
            );
            assert!(
                r.stale_paints > 10,
                "{fault:?}, seed {seed:#x}: the oracle must catch it"
            );
        }
    }
}

#[test]
fn simulation_is_deterministic() {
    let a = simulate(SEEDS[0], 500, &mut PaintGate::new());
    let b = simulate(SEEDS[0], 500, &mut PaintGate::new());
    assert_eq!(
        (a.events, a.accepted, a.dropped),
        (b.events, b.accepted, b.dropped)
    );
}
