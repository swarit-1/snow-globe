//! Press/release pairing for the hotkey checks (PLAN.md M1 1a checks 1 and 9; PRD §5.4 "Hold
//! hotkey: listen; release to ask").
//!
//! The app logs one [`EdgeEvent`] per global-shortcut handler call. For a release it records:
//! - whether the hotkey's modifiers still read as held (`NSEvent.modifierFlags`) when the handler
//!   ran;
//! - when a 2 ms poller first saw the modifiers go up during the hold;
//! - with `--key-state`, whether the main key still read as down;
//! - the order the owner was asked to use.
//!
//! [`classify`] turns these into the release order (key-first or modifier-first) and the
//! trigger (did the release fire when the key went up, or when the modifier did). Each
//! classification names its [`Basis`], so a trigger inferred from the instructed order is never
//! mistaken for a measured one.

use serde::{Deserialize, Serialize};

use crate::jsonl::Status;
use crate::stats::{Summary, summarize};

/// Which edge the handler saw.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Edge {
    Pressed,
    Released,
}

/// The order in which the key and its modifiers were let go.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Order {
    /// The main key went up while the modifiers were still held.
    KeyFirst,
    /// The modifiers went up first, then the main key.
    ModifierFirst,
    /// Within [`SIMULTANEOUS_MS`] of each other, with nothing to tell them apart; counted for
    /// neither order.
    Simultaneous,
}

/// What made the release event fire.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Trigger {
    /// The main key going up.
    KeyUp,
    /// The modifier going up while the main key was still down.
    ModifierUp,
    Unknown,
}

/// What a release's classification rests on.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Basis {
    /// The hotkey's modifiers still read as held when the release handler ran, so the release
    /// can only have come from the key going up.
    ModifiersHeld,
    /// The poller saw the modifiers go up at least [`SIMULTANEOUS_MS`] before the release, so
    /// the release came with the key going up later.
    PollerGap,
    /// The main key's own state (`CGEventSourceKeyState`, `--key-state`) at the release.
    KeyState,
    /// Inferred, not measured: the modifiers went up within [`SIMULTANEOUS_MS`] of the release
    /// (or just before it, before the poller saw them) in a trial where the owner was asked to
    /// let go of the modifiers and keep the key down about 1 s more. The release is then taken
    /// to have fired on the modifier going up. A trial where the owner let go of both at once
    /// would be misattributed, so the per-trial `release_after_modifiers_ms` is kept.
    RequestedOrder,
    /// No modifier reading at the handler, and the poller never saw the modifiers go up during
    /// the hold (records written before `modifiers_held_at_release` existed).
    PollerOnly,
    /// Not enough to tell.
    Undetermined,
}

/// One handler call as logged by the app.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EdgeEvent {
    pub edge: Edge,
    /// Host clock at handler entry.
    pub host_ns: u64,
    /// For a release: when the poller first saw the hotkey's modifiers up during the hold.
    #[serde(default)]
    pub modifiers_up_ns: Option<u64>,
    /// For a release: whether every hotkey modifier still read as held (`NSEvent.modifierFlags`)
    /// at handler entry.
    #[serde(default)]
    pub modifiers_held_at_release: Option<bool>,
    /// For a release: whether the main key still read as down at the handler, when the key
    /// state was readable during the hold (`None` when it never read as down, i.e. unreliable).
    #[serde(default)]
    pub key_down_at_release: Option<bool>,
    /// The order the owner was asked to use, in guided trials.
    #[serde(default)]
    pub requested: Option<Order>,
}

/// Releases arriving less than this long after the modifiers went up are "close" to it: either
/// simultaneous or fired by the modifier going up.
pub const SIMULTANEOUS_MS: f64 = 20.0;

/// A guided check 1 run stops after this many trials per `--per-order`, whatever was observed.
pub const MAX_TRIALS_PER_ORDER: usize = 3;

/// One release's classification.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Classified {
    pub order: Order,
    pub trigger: Trigger,
    pub basis: Basis,
}

fn c(order: Order, trigger: Trigger, basis: Basis) -> Classified {
    Classified {
        order,
        trigger,
        basis,
    }
}

/// Classifies one release (PLAN.md M1 1a check 1, "including when the modifier is released
/// first"; see [`Basis`] for what each outcome rests on).
pub fn classify(
    release_ns: u64,
    modifiers_up_ns: Option<u64>,
    modifiers_held_at_release: Option<bool>,
    key_down_at_release: Option<bool>,
    requested: Option<Order>,
) -> Classified {
    use {Basis::*, Order::*, Trigger::*};
    if modifiers_held_at_release == Some(true) {
        return c(KeyFirst, KeyUp, ModifiersHeld);
    }
    let gap_ms = modifiers_up_ns.map(|u| (release_ns as f64 - u as f64) / 1e6);
    if let Some(g) = gap_ms
        && g >= SIMULTANEOUS_MS
    {
        return match key_down_at_release {
            Some(true) => c(ModifierFirst, Unknown, KeyState),
            Some(false) => c(ModifierFirst, KeyUp, KeyState),
            None => c(ModifierFirst, KeyUp, PollerGap),
        };
    }
    // The modifiers went up close to the release: the poller saw it within SIMULTANEOUS_MS, or
    // had not seen it yet although the modifiers read as up at the handler.
    if gap_ms.is_some() || modifiers_held_at_release == Some(false) {
        return match key_down_at_release {
            Some(true) => c(ModifierFirst, ModifierUp, KeyState),
            Some(false) => c(Simultaneous, KeyUp, KeyState),
            None if requested == Some(ModifierFirst) => {
                c(ModifierFirst, ModifierUp, RequestedOrder)
            }
            None => c(Simultaneous, Unknown, Undetermined),
        };
    }
    c(KeyFirst, KeyUp, PollerOnly)
}

/// One press and its release.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Trial {
    pub index: usize,
    pub press_ns: u64,
    pub release_ns: Option<u64>,
    pub hold_ms: Option<f64>,
    pub observed: Option<Order>,
    pub trigger: Option<Trigger>,
    pub basis: Option<Basis>,
    pub requested: Option<Order>,
    /// Release time minus the time the modifiers were first seen up, in ms.
    pub release_after_modifiers_ms: Option<f64>,
}

/// The result of pairing a sequence of edges.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Pairing {
    pub trials: Vec<Trial>,
    /// A press arrived while the previous press had no release (a missed release or a repeat).
    pub pressed_while_held: usize,
    /// A release arrived with no press open.
    pub orphan_releases: usize,
    /// The sequence ended with a press still open.
    pub open_at_end: usize,
}

/// Pairs edges in time order.
pub fn pair(events: &[EdgeEvent]) -> Pairing {
    let mut sorted: Vec<&EdgeEvent> = events.iter().collect();
    sorted.sort_by_key(|e| e.host_ns);
    let mut trials: Vec<Trial> = Vec::new();
    let mut open: Option<usize> = None;
    let mut pressed_while_held = 0;
    let mut orphan_releases = 0;
    for e in sorted {
        match e.edge {
            Edge::Pressed => {
                if open.is_some() {
                    pressed_while_held += 1;
                }
                trials.push(Trial {
                    index: trials.len(),
                    press_ns: e.host_ns,
                    release_ns: None,
                    hold_ms: None,
                    observed: None,
                    trigger: None,
                    basis: None,
                    requested: e.requested,
                    release_after_modifiers_ms: None,
                });
                open = Some(trials.len() - 1);
            }
            Edge::Released => match open.take() {
                Some(i) => {
                    let t = &mut trials[i];
                    if t.requested.is_none() {
                        t.requested = e.requested;
                    }
                    let k = classify(
                        e.host_ns,
                        e.modifiers_up_ns,
                        e.modifiers_held_at_release,
                        e.key_down_at_release,
                        t.requested,
                    );
                    t.release_ns = Some(e.host_ns);
                    t.hold_ms = Some((e.host_ns as f64 - t.press_ns as f64) / 1e6);
                    t.observed = Some(k.order);
                    t.trigger = Some(k.trigger);
                    t.basis = Some(k.basis);
                    t.release_after_modifiers_ms = e
                        .modifiers_up_ns
                        .map(|u| (e.host_ns as f64 - u as f64) / 1e6);
                }
                None => orphan_releases += 1,
            },
        }
    }
    Pairing {
        open_at_end: usize::from(open.is_some()),
        trials,
        pressed_while_held,
        orphan_releases,
    }
}

/// Check 1's guided sequence: ask for key-first releases until `per_order` have been observed,
/// then modifier-first ones until as many have been observed. Returns the order to ask for next,
/// or `None` when both orders are done or `max_trials` releases have happened.
pub fn next_request(
    per_order: usize,
    max_trials: usize,
    released: usize,
    key_first: usize,
    modifier_first: usize,
) -> Option<Order> {
    let done = key_first >= per_order && modifier_first >= per_order;
    if done || released >= max_trials {
        return None;
    }
    Some(if key_first < per_order {
        Order::KeyFirst
    } else {
        Order::ModifierFirst
    })
}

/// Check 1's verdict.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct HotkeyVerdict {
    pub status: Status,
    pub trials: usize,
    pub released: usize,
    pub key_first: usize,
    pub modifier_first: usize,
    pub simultaneous: usize,
    pub trigger_key_up: usize,
    pub trigger_modifier_up: usize,
    pub trigger_unknown: usize,
    /// Modifier-up triggers inferred from the instructed order rather than measured with
    /// `--key-state` ([`Basis::RequestedOrder`]).
    pub trigger_modifier_up_inferred: usize,
    pub pressed_while_held: usize,
    pub orphan_releases: usize,
    pub open_at_end: usize,
    /// Trials where the owner was asked for one order and did the other.
    pub order_mismatches: usize,
    pub hold_ms: Option<Summary>,
    /// `release_after_modifiers_ms` over the modifier-first trials: about 0 when the release
    /// fires on the modifier going up, about the extra hold when it fires on the key going up.
    pub modifier_first_release_after_modifiers_ms: Option<Summary>,
    pub reasons: Vec<String>,
    /// Facts the reader needs that are not failures.
    pub notes: Vec<String>,
}

/// Check 1: every press yields exactly one release with no repeats, and both orders were
/// exercised at least `min_per_order` times.
pub fn verdict(p: &Pairing, min_per_order: usize) -> HotkeyVerdict {
    let count = |o: Order| p.trials.iter().filter(|t| t.observed == Some(o)).count();
    let trig = |g: Trigger| p.trials.iter().filter(|t| t.trigger == Some(g)).count();
    let released = p.trials.iter().filter(|t| t.release_ns.is_some()).count();
    let order_mismatches = p
        .trials
        .iter()
        .filter(|t| match (t.requested, t.observed) {
            (Some(r), Some(o)) => o != Order::Simultaneous && r != o,
            _ => false,
        })
        .count();
    let inferred = p
        .trials
        .iter()
        .filter(|t| t.basis == Some(Basis::RequestedOrder))
        .count();
    let holds: Vec<f64> = p.trials.iter().filter_map(|t| t.hold_ms).collect();
    let mf_gaps: Vec<f64> = p
        .trials
        .iter()
        .filter(|t| t.observed == Some(Order::ModifierFirst))
        .filter_map(|t| t.release_after_modifiers_ms)
        .collect();
    let (key_first, modifier_first) = (count(Order::KeyFirst), count(Order::ModifierFirst));
    let mut reasons = Vec::new();
    // The last press may still be open only if the run was cut short; that is not a failure by
    // itself, but a press followed by another press is.
    if p.pressed_while_held > 0 {
        reasons.push(format!(
            "{} press(es) arrived while the previous press had no release",
            p.pressed_while_held
        ));
    }
    if p.orphan_releases > 0 {
        reasons.push(format!("{} release(s) with no press", p.orphan_releases));
    }
    let failed = !reasons.is_empty();
    if key_first < min_per_order {
        reasons.push(format!(
            "only {key_first} key-first trial(s), need {min_per_order}"
        ));
    }
    if modifier_first < min_per_order {
        reasons.push(format!(
            "only {modifier_first} modifier-first trial(s), need {min_per_order}"
        ));
    }
    let mut notes = Vec::new();
    if inferred > 0 {
        notes.push(format!(
            "{inferred} modifier-first release(s) came within {SIMULTANEOUS_MS} ms of the \
             modifiers going up and are attributed to the modifier going up from the instructed \
             order (the owner kept the key down); --key-state measures it instead"
        ));
    }
    let status = if failed {
        Status::Fail
    } else if key_first >= min_per_order && modifier_first >= min_per_order {
        Status::Pass
    } else {
        Status::Incomplete
    };
    HotkeyVerdict {
        status,
        trials: p.trials.len(),
        released,
        key_first,
        modifier_first,
        simultaneous: count(Order::Simultaneous),
        trigger_key_up: trig(Trigger::KeyUp),
        trigger_modifier_up: trig(Trigger::ModifierUp),
        trigger_unknown: trig(Trigger::Unknown),
        trigger_modifier_up_inferred: inferred,
        pressed_while_held: p.pressed_while_held,
        orphan_releases: p.orphan_releases,
        open_at_end: p.open_at_end,
        order_mismatches,
        hold_ms: summarize(&holds),
        modifier_first_release_after_modifiers_ms: summarize(&mf_gaps),
        reasons,
        notes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: u64 = 1_000_000;

    fn press(t: u64) -> EdgeEvent {
        EdgeEvent {
            edge: Edge::Pressed,
            host_ns: t * MS,
            modifiers_up_ns: None,
            modifiers_held_at_release: None,
            key_down_at_release: None,
            requested: None,
        }
    }

    fn release(t: u64, mods_up: Option<u64>, key_down: Option<bool>) -> EdgeEvent {
        EdgeEvent {
            edge: Edge::Released,
            host_ns: t * MS,
            modifiers_up_ns: mods_up.map(|m| m * MS),
            modifiers_held_at_release: None,
            key_down_at_release: key_down,
            requested: None,
        }
    }

    fn held(mut e: EdgeEvent, h: bool) -> EdgeEvent {
        e.modifiers_held_at_release = Some(h);
        e
    }

    fn asked(mut e: EdgeEvent, o: Order) -> EdgeEvent {
        e.requested = Some(o);
        e
    }

    fn pair_of(c: Classified) -> (Order, Trigger) {
        (c.order, c.trigger)
    }

    #[test]
    fn classifies_release_orders_and_triggers() {
        use Order::*;
        use Trigger::*;
        // Old-format records (no handler reading): the poller never saw the modifiers go up.
        assert_eq!(
            classify(100 * MS, None, None, None, None),
            c(KeyFirst, KeyUp, Basis::PollerOnly)
        );
        assert_eq!(
            pair_of(classify(1100 * MS, Some(100 * MS), None, Some(false), None)),
            (ModifierFirst, KeyUp)
        );
        assert_eq!(
            classify(102 * MS, Some(100 * MS), None, Some(true), None),
            c(ModifierFirst, ModifierUp, Basis::KeyState)
        );
        assert_eq!(
            classify(105 * MS, Some(100 * MS), None, None, None),
            c(Simultaneous, Unknown, Basis::Undetermined)
        );
        // Modifiers held at the handler: the key went up first, whatever the poller saw.
        assert_eq!(
            classify(
                105 * MS,
                Some(104 * MS),
                Some(true),
                None,
                Some(ModifierFirst)
            ),
            c(KeyFirst, KeyUp, Basis::ModifiersHeld)
        );
        // The modifier went up one second before the release: the key fired it.
        assert_eq!(
            classify(
                1100 * MS,
                Some(100 * MS),
                Some(false),
                None,
                Some(ModifierFirst)
            ),
            c(ModifierFirst, KeyUp, Basis::PollerGap)
        );
        // Modifiers up at the handler but not yet seen by the poller, key-first requested: the
        // owner let go of both at about the same time.
        assert_eq!(
            classify(105 * MS, None, Some(false), None, Some(KeyFirst)),
            c(Simultaneous, Unknown, Basis::Undetermined)
        );
    }

    /// The reviewer's case (PLAN.md M1 1a check 1, the outcome where the release fires when the
    /// modifier goes up while Space is still held): 10 key-first trials, then 10 requested
    /// modifier-first trials whose release arrives 3 ms after the poller saw the modifiers go up
    /// (even trials) or before the poller saw them at all (odd trials). Before the fix this was
    /// Incomplete with 0 modifier-first trials, 5 of them miscounted as key-first.
    #[test]
    fn release_on_modifier_up_is_counted_modifier_first() {
        let mut ev = Vec::new();
        let mut t = 1000u64;
        for _ in 0..10 {
            ev.push(asked(press(t), Order::KeyFirst));
            ev.push(asked(
                held(release(t + 900, None, None), true),
                Order::KeyFirst,
            ));
            t += 3000;
        }
        for k in 0..10u64 {
            ev.push(asked(press(t), Order::ModifierFirst));
            let mods_up = t + 900;
            let r = if k % 2 == 0 {
                release(mods_up + 3, Some(mods_up), None)
            } else {
                release(mods_up + 1, None, None)
            };
            ev.push(asked(held(r, false), Order::ModifierFirst));
            t += 3000;
        }
        let p = pair(&ev);
        let v = verdict(&p, 10);
        assert_eq!(v.status, Status::Pass, "{v:?}");
        assert_eq!((v.key_first, v.modifier_first, v.simultaneous), (10, 10, 0));
        assert_eq!(v.order_mismatches, 0);
        assert_eq!(
            (v.trigger_modifier_up, v.trigger_modifier_up_inferred),
            (10, 10)
        );
        assert_eq!(v.trigger_key_up, 10);
        assert!(v.notes[0].contains("instructed order"), "{:?}", v.notes);
        assert!(
            p.trials[10..]
                .iter()
                .all(|t| t.basis == Some(Basis::RequestedOrder))
        );
    }

    /// The other outcome: the release fires when Space goes up, about 1 s after the modifiers.
    #[test]
    fn release_on_key_up_after_the_modifier_is_measured_by_the_poller_gap() {
        let mut ev = Vec::new();
        let mut t = 0u64;
        for k in 0..4u64 {
            let o = if k < 2 {
                Order::KeyFirst
            } else {
                Order::ModifierFirst
            };
            ev.push(asked(press(t), o));
            let r = if k < 2 {
                held(release(t + 800, None, None), true)
            } else {
                held(release(t + 1800, Some(t + 800), None), false)
            };
            ev.push(asked(r, o));
            t += 3000;
        }
        let p = pair(&ev);
        let v = verdict(&p, 2);
        assert_eq!(v.status, Status::Pass, "{v:?}");
        assert_eq!((v.key_first, v.modifier_first), (2, 2));
        assert_eq!((v.trigger_key_up, v.trigger_modifier_up), (4, 0));
        assert_eq!(v.trigger_modifier_up_inferred, 0);
        assert!(v.notes.is_empty());
        let gaps = v
            .modifier_first_release_after_modifiers_ms
            .expect("modifier-first gaps");
        assert_eq!((gaps.n, gaps.min), (2, 1000.0));
        assert_eq!(v.hold_ms.as_ref().map(|s| s.max), Some(1800.0));
        assert_eq!(verdict(&p, 3).status, Status::Incomplete);
    }

    #[test]
    fn key_state_measures_the_trigger_without_the_request() {
        let ev = vec![
            press(0),
            held(release(903, Some(900), Some(true)), false),
            press(3000),
            held(release(3902, Some(3900), Some(false)), false),
        ];
        let p = pair(&ev);
        assert_eq!(p.trials[0].basis, Some(Basis::KeyState));
        assert_eq!(
            (p.trials[0].observed, p.trials[0].trigger),
            (Some(Order::ModifierFirst), Some(Trigger::ModifierUp))
        );
        assert_eq!(p.trials[1].observed, Some(Order::Simultaneous));
        assert_eq!(verdict(&p, 0).trigger_modifier_up_inferred, 0);
    }

    #[test]
    fn a_missing_release_fails() {
        let ev = vec![press(0), press(900), release(1500, None, None)];
        let p = pair(&ev);
        assert_eq!(p.pressed_while_held, 1);
        let v = verdict(&p, 0);
        assert_eq!(v.status, Status::Fail);
        assert!(v.reasons[0].contains("no release"));
    }

    #[test]
    fn an_orphan_release_fails_and_unsorted_input_is_sorted() {
        let ev = vec![release(10, None, None), press(0), release(5, None, None)];
        // Sorted: press 0, release 5, release 10 (orphan).
        let p = pair(&ev);
        assert_eq!((p.trials.len(), p.orphan_releases), (1, 1));
        assert_eq!(verdict(&p, 0).status, Status::Fail);
    }

    #[test]
    fn counts_order_mismatches_against_the_request() {
        let ev = vec![
            asked(press(0), Order::ModifierFirst),
            held(release(900, None, None), true),
        ];
        let v = verdict(&pair(&ev), 0);
        assert_eq!(v.order_mismatches, 1);
        assert_eq!(v.status, Status::Pass);
    }

    #[test]
    fn guided_runs_continue_until_both_orders_are_observed() {
        // Key-first until 10 are observed, then modifier-first, capped at 30 releases.
        assert_eq!(next_request(10, 30, 0, 0, 0), Some(Order::KeyFirst));
        assert_eq!(next_request(10, 30, 12, 9, 0), Some(Order::KeyFirst));
        assert_eq!(next_request(10, 30, 12, 10, 0), Some(Order::ModifierFirst));
        // A key-first request observed as modifier-first still counts for that order.
        assert_eq!(next_request(10, 30, 19, 10, 9), Some(Order::ModifierFirst));
        assert_eq!(next_request(10, 30, 20, 10, 10), None);
        assert_eq!(next_request(10, 30, 30, 10, 4), None);
    }
}
