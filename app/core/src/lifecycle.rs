//! Query lifecycle: generation numbers, cancellation and the paint gate (PRD §10.2, §10.3).
//!
//! - Every key press starts a new **generation** ([`GenCounter::begin`]); everything started for
//!   an older generation is cancelled ([`CancelToken::is_cancelled`] turns true at once).
//! - Every paint is checked by the [`PaintGate`] immediately before compositing, against the
//!   current `(query_gen, scene_hash, tier)`: a result from an older generation, for a different
//!   scene (the valid time was scrubbed, AM/PM flipped, or the viewpoint changed), or for a tier
//!   no newer than the one already on screen is dropped as stale.
//! - **Esc** (PRD §5.2: any state → idle) is [`escape`]: it cancels the generation's token *and*
//!   dismisses the gate, so nothing paints until the next key press. Cancelling the token alone
//!   stops stages that poll it, but a result already in flight would still be accepted.
//! - The key-mash simulation in `tests/keymash.rs` drives this API through ≥ 10,000 randomised
//!   cycles with injected stage delays on a simulated clock and asserts zero stale paints.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

use crate::query::Query;

/// A query generation number (PRD §10.2 "One generation number per query").
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct QueryGen(pub u64);

/// Hands out generations; the latest one is current.
#[derive(Debug, Clone, Default)]
pub struct GenCounter {
    current: Arc<AtomicU64>,
}

impl GenCounter {
    /// A counter at generation 0 (nothing started yet).
    pub fn new() -> GenCounter {
        GenCounter::default()
    }

    /// Start a new generation (a key press) and return it with its cancellation token.
    /// Every older token is cancelled by this call.
    pub fn begin(&self) -> (QueryGen, CancelToken) {
        let generation = self.current.fetch_add(1, Ordering::SeqCst) + 1;
        (
            QueryGen(generation),
            CancelToken {
                generation,
                current: Arc::clone(&self.current),
                explicit: Arc::new(AtomicBool::new(false)),
            },
        )
    }

    /// The current generation.
    pub fn current(&self) -> QueryGen {
        QueryGen(self.current.load(Ordering::SeqCst))
    }
}

/// Cancellation for the work of one generation. Cheap to clone and send across threads.
#[derive(Debug, Clone)]
pub struct CancelToken {
    generation: u64,
    current: Arc<AtomicU64>,
    explicit: Arc<AtomicBool>,
}

impl CancelToken {
    /// The generation this token belongs to.
    pub fn generation(&self) -> QueryGen {
        QueryGen(self.generation)
    }

    /// True once a newer generation has begun, or after [`CancelToken::cancel`].
    pub fn is_cancelled(&self) -> bool {
        self.explicit.load(Ordering::SeqCst)
            || self.current.load(Ordering::SeqCst) != self.generation
    }

    /// Cancel this generation's work without starting a new one: stages that poll the token
    /// stop. It does not close the paint gate, so a result already in flight could still paint;
    /// Esc must use [`escape`], which also calls [`PaintGate::dismiss`].
    pub fn cancel(&self) {
        self.explicit.store(true, Ordering::SeqCst);
    }
}

/// A stable 64-bit hash of what a frame shows (PRD §10.3 `scene_hash`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SceneHash(pub u64);

impl SceneHash {
    /// FNV-1a over the given parts, separated so that ("ab","c") ≠ ("a","bc"). Stable across runs
    /// and Rust versions (unlike `std`'s `DefaultHasher`).
    pub fn of_parts(parts: &[&[u8]]) -> SceneHash {
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for part in parts {
            for &b in part.iter().chain(&[0xff]) {
                h ^= u64::from(b);
                h = h.wrapping_mul(0x0000_0100_0000_01b3);
            }
        }
        SceneHash(h)
    }

    /// The scene of a query: viewpoint, valid instant and overrides.
    pub fn of_query(q: &Query) -> SceneHash {
        let instant = q.valid_time_utc.timestamp().to_le_bytes();
        let overrides = serde_json::to_string(&q.overrides).unwrap_or_default();
        SceneHash::of_parts(&[q.viewpoint_id.as_bytes(), &instant, overrides.as_bytes()])
    }
}

/// The three rendering tiers (PRD §9).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    /// The closest real frame.
    Tier0,
    /// The relit plate.
    Tier1,
    /// Guarded refinement.
    Tier2,
}

/// What a finished stage wants to paint.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PaintKey {
    /// Generation the result was made for.
    pub generation: QueryGen,
    /// Scene the result shows.
    pub scene: SceneHash,
    /// Its tier.
    pub tier: Tier,
}

/// What is on screen now.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct GateState {
    /// Current generation.
    pub generation: QueryGen,
    /// Current scene, once the query has been parsed.
    pub scene: Option<SceneHash>,
    /// Highest tier painted for this generation and scene.
    pub shown: Option<Tier>,
    /// True after Esc: the overlay is idle and nothing paints until the next key press.
    pub dismissed: bool,
}

/// Why a paint was dropped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum StaleReason {
    /// Made for an older (or not yet current) generation.
    Generation,
    /// Made for another scene (scrubbed, flipped, or not parsed yet).
    Scene,
    /// A tier at or below the one already on screen.
    Tier,
    /// The overlay was dismissed (Esc) after the query began.
    Dismissed,
}

/// The gate's verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PaintDecision {
    /// Paint it.
    Accept,
    /// Drop it.
    DropStale(StaleReason),
}

/// The paint gate (PRD §10.3), checked immediately before compositing.
#[derive(Debug, Clone)]
pub struct PaintGate {
    state: GateState,
}

impl Default for PaintGate {
    fn default() -> Self {
        PaintGate {
            state: GateState {
                generation: QueryGen(0),
                scene: None,
                shown: None,
                dismissed: false,
            },
        }
    }
}

impl PaintGate {
    /// The pure rule: nothing after Esc; otherwise only the current generation and scene, and
    /// only a newer tier.
    pub fn check(current: &GateState, incoming: &PaintKey) -> PaintDecision {
        if incoming.generation != current.generation {
            return PaintDecision::DropStale(StaleReason::Generation);
        }
        if current.dismissed {
            return PaintDecision::DropStale(StaleReason::Dismissed);
        }
        if current.scene != Some(incoming.scene) {
            return PaintDecision::DropStale(StaleReason::Scene);
        }
        if current.shown.is_some_and(|shown| incoming.tier <= shown) {
            return PaintDecision::DropStale(StaleReason::Tier);
        }
        PaintDecision::Accept
    }

    /// A gate with nothing current.
    pub fn new() -> PaintGate {
        PaintGate::default()
    }

    /// The state the gate checks against.
    pub fn state(&self) -> GateState {
        self.state
    }

    /// A key press began generation `generation`: nothing older may paint, and no scene is known yet.
    pub fn begin(&mut self, generation: QueryGen) {
        self.state = GateState {
            generation,
            scene: None,
            shown: None,
            dismissed: false,
        };
    }

    /// Esc: the overlay goes idle (PRD §5.2). Every paint is dropped, and late parse results
    /// cannot set a scene, until the next [`PaintGate::begin`].
    pub fn dismiss(&mut self) {
        self.state.scene = None;
        self.state.shown = None;
        self.state.dismissed = true;
    }

    /// The query for `generation` resolved to `scene`, or the user scrubbed or flipped to a new scene.
    /// Ignored if `generation` is not current or the overlay was dismissed. Tiers start again for
    /// the new scene.
    pub fn set_scene(&mut self, generation: QueryGen, scene: SceneHash) {
        if generation == self.state.generation
            && !self.state.dismissed
            && self.state.scene != Some(scene)
        {
            self.state.scene = Some(scene);
            self.state.shown = None;
        }
    }

    /// Check a paint and, if accepted, record its tier as shown.
    pub fn try_paint(&mut self, incoming: &PaintKey) -> PaintDecision {
        let decision = PaintGate::check(&self.state, incoming);
        if decision == PaintDecision::Accept {
            self.state.shown = Some(incoming.tier);
        }
        decision
    }
}

/// Esc (PRD §5.2, §10.3): cancel the work of `token`'s generation and dismiss the gate, so that
/// no result of the dismissed query paints, however late it arrives.
pub fn escape(token: &CancelToken, gate: &mut PaintGate) {
    token.cancel();
    gate.dismiss();
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: SceneHash = SceneHash(1);
    const B: SceneHash = SceneHash(2);

    #[test]
    fn begin_cancels_older_tokens() {
        let c = GenCounter::new();
        let (g1, t1) = c.begin();
        assert!(!t1.is_cancelled());
        let (g2, t2) = c.begin();
        assert!(g2 > g1);
        assert!(t1.is_cancelled());
        assert!(!t2.is_cancelled());
        t2.cancel();
        assert!(t2.is_cancelled());
        assert_eq!(c.current(), g2);
    }

    #[test]
    fn gate_rules() {
        let mut g = PaintGate::new();
        g.begin(QueryGen(1));
        let key = |generation, scene, tier| PaintKey {
            generation: QueryGen(generation),
            scene,
            tier,
        };
        assert_eq!(
            g.try_paint(&key(1, A, Tier::Tier0)),
            PaintDecision::DropStale(StaleReason::Scene),
            "not parsed yet"
        );
        g.set_scene(QueryGen(1), A);
        assert_eq!(g.try_paint(&key(1, A, Tier::Tier1)), PaintDecision::Accept);
        assert_eq!(
            g.try_paint(&key(1, A, Tier::Tier0)),
            PaintDecision::DropStale(StaleReason::Tier)
        );
        assert_eq!(
            g.try_paint(&key(1, A, Tier::Tier1)),
            PaintDecision::DropStale(StaleReason::Tier)
        );
        assert_eq!(
            g.try_paint(&key(1, B, Tier::Tier2)),
            PaintDecision::DropStale(StaleReason::Scene)
        );
        g.set_scene(QueryGen(1), B);
        assert_eq!(
            g.try_paint(&key(1, A, Tier::Tier2)),
            PaintDecision::DropStale(StaleReason::Scene)
        );
        assert_eq!(g.try_paint(&key(1, B, Tier::Tier1)), PaintDecision::Accept);
        g.begin(QueryGen(2));
        assert_eq!(
            g.try_paint(&key(1, B, Tier::Tier2)),
            PaintDecision::DropStale(StaleReason::Generation)
        );
        g.set_scene(QueryGen(1), A);
        assert_eq!(
            g.state().scene,
            None,
            "an old generation cannot set the scene"
        );
    }

    #[test]
    fn a_late_paint_after_esc_is_dropped() {
        let counter = GenCounter::new();
        let mut gate = PaintGate::new();
        let (g, token) = counter.begin();
        gate.begin(g);
        gate.set_scene(g, A);
        let key = |tier| PaintKey {
            generation: g,
            scene: A,
            tier,
        };
        assert_eq!(gate.try_paint(&key(Tier::Tier0)), PaintDecision::Accept);
        escape(&token, &mut gate);
        assert!(token.is_cancelled());
        // The adversarial review's case: before the fix this Tier 2 paint was accepted.
        assert_eq!(
            gate.try_paint(&key(Tier::Tier2)),
            PaintDecision::DropStale(StaleReason::Dismissed)
        );
        // A late parse result cannot reopen the gate either.
        gate.set_scene(g, B);
        assert_eq!(gate.state().scene, None);
        assert_eq!(
            gate.try_paint(&PaintKey {
                generation: g,
                scene: B,
                tier: Tier::Tier1
            }),
            PaintDecision::DropStale(StaleReason::Dismissed)
        );
        // The next key press opens it again.
        let (g2, _) = counter.begin();
        gate.begin(g2);
        gate.set_scene(g2, A);
        assert_eq!(
            gate.try_paint(&PaintKey {
                generation: g2,
                scene: A,
                tier: Tier::Tier0
            }),
            PaintDecision::Accept
        );
    }

    #[test]
    fn scene_hash_is_stable_and_separates_parts() {
        assert_eq!(
            SceneHash::of_parts(&[b"rainier.paradise-mountain", b"x"]),
            SceneHash::of_parts(&[b"rainier.paradise-mountain", b"x"])
        );
        assert_ne!(
            SceneHash::of_parts(&[b"ab", b"c"]),
            SceneHash::of_parts(&[b"a", b"bc"])
        );
        // Pinned value: FNV-1a must not change between builds (cache keys depend on it).
        assert_eq!(SceneHash::of_parts(&[b""]).0, 0xaf64_724c_8602_eb6e);
    }
}
