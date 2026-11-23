//! Tier 0: the closest real frame (PRD §9.1; ticket M1-08).
//!
//! Within 300 ms p95 of key-up (PRD §10.1), Snow Globe shows a real photo from the same camera
//! whose Sun position and time of year are closest to the asked valid time, labelled as a real
//! photo with when it was taken and what it was matched on (PRD §5.3). D-012 row 11: when Tier 1
//! is ready within about 100 ms the overlay skips painting it, but Tier 0 still runs and is
//! measured.
//!
//! | Module | What | PRD |
//! |---|---|---|
//! | [`camera`] | the camera facts from the manifests: view azimuth, attribution, archive | App. A, §15.4 |
//! | [`heldout`] | the held-out rules, from the split eval calls current: the archive edge, the sealed window and every other held-out period | D-019, D-022, D-070, D-085 |
//! | [`sidecar`] | one capture sidecar → a library record or an exclusion | §8.6, §8.7 |
//! | [`library`] | the index over `frames/`, `thumbs/` and `archive/`, its report, its cache, and the checked path an image is opened by | §9.1 "Library" |
//! | [`matcher`] | the descriptor, the documented distance, and the match | §9.1 "Query" |
//! | [`decode`] | the 48 h prefetch plan, background JPEG decode, the decoded-set hand-over, and the fallback to the next frame | §9.1 "Latency" |
//! | [`caption`] | "REAL PHOTO, CLOSEST MATCH · taken … · matched on …" | §5.3 |
//! | [`ask`] | the `snow-globe-ask` command: utterance in, photo out | §6.3 |
//! | [`tool`] | the `snow-globe-tier0` command: index report and the measured run | §10.4 |
//!
//! **Data rules** (CLAUDE.md): the library reads sidecars only; it never opens `quarantine/` and
//! never follows a symbolic link (the image beside a sidecar included); it never takes an archived
//! frame dated on or after 1 Jun 2026 by its local or UTC date, any frame dated in the sealed
//! window (15 Dec 2026 – 14 Jan 2027) or another held-out period of the split, and takes only our
//! own captures at Rainier (D-019). A match sees only frames available at its cut-off: taken
//! before it and fetched by it (PRD §9.1 "Leakage", §13.1). Its only write is its own cache file
//! under the data dir's `cache/tier0/`.
//!
//! **Not yet:** the weather-class part of the descriptor (extractor v0 labels the library, and the
//! forecast file gives the query's class: TODO(M1.5)); the camera-fault class and the Same view
//! gate, which need pixels and plates (TODO(M1.5), TODO(M2)); fitted distance weights and the
//! "no close analog" stratum, and one distance shared with eval's R3 analog (the
//! pre-registration, TODO(M2)); starting the prefetch on partial transcripts (the app shell,
//! TODO(M1)); and the overlay's swap and paint of the decoded frame (the M1 overlay).

pub mod ask;
pub mod camera;
pub mod caption;
pub mod decode;
pub mod heldout;
pub mod library;
pub mod matcher;
pub mod sidecar;
pub mod tool;

pub use camera::{CameraInfo, CameraTable};
pub use library::{CacheMode, Entry, Library, LibraryReport};
pub use matcher::{MatchOutcome, MatchRequest, find};
