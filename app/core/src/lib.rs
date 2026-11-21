//! # snow-globe-core
//!
//! The platform-independent core of Snow Globe (PRD §6.1): everything between the final transcript
//! and the renderer that can be tested without a microphone, a GPU or the network.
//!
//! | Module | What | PRD |
//! |---|---|---|
//! | [`normalize`] | lower-case, punctuation, contractions, filler lists | §7.3 step 1 |
//! | [`timeparse`] | the hand-written time grammar | §7.3 step 2 |
//! | [`places`] | alias table from the viewpoint manifests; alias → exact → fuzzy → phonetic matching | §7.5 |
//! | [`phonetic`] | Double Metaphone keys | §7.5 |
//! | [`resolve`] | the time rules, in the place's zone; DST and horizon | §7.4 |
//! | [`tzutil`] | wall time → instant with the DST gap/fold policy | §7.4 |
//! | [`solar`] | Sun and Moon position over `oculus-core` and solar event search | §7.4, §8.4 |
//! | [`parser`] | the fast path, utterance → [`Query`], plus follow-up sessions | §7.3 |
//! | [`query`] | the query and its JSON shape | §7.3 |
//! | [`trace`] | per-query JSON-lines trace with no transcript text | §10.5 |
//! | [`lifecycle`] | query generations, cancellation, the paint gate, Esc | §5.2, §10.2, §10.3 |
//! | [`datadir`] | `$SNOW_GLOBE_DATA_DIR` with the repo and iCloud safety checks | CLAUDE.md |
//! | [`cli`] | the `snow-globe-parse` command | §7.3 |

pub mod cli;
pub mod datadir;
pub mod lifecycle;
pub mod normalize;
pub mod parser;
pub mod phonetic;
pub mod places;
pub mod query;
pub mod resolve;
pub mod solar;
pub mod timeparse;
pub mod trace;
pub mod tzutil;

pub use parser::{LastPlace, ParseContext, ParseError, Parser, Session};
pub use query::Query;
pub use resolve::TimeConfig;
