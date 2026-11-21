//! The alias table from the real manifests (PRD §7.5): loading `viewpoints/manifests/` at run time
//! gives the same table as the copy compiled into the crate, and every alias in every manifest
//! parses to its own place through the full parser (fillers and the time grammar never eat it).

use chrono::{DateTime, Utc};
use snow_globe_core::places::PlaceTable;
use snow_globe_core::query::PlaceStage;
use snow_globe_core::{ParseContext, Parser, TimeConfig};
use std::path::Path;

fn manifest_dir() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../viewpoints/manifests")
}

#[test]
fn runtime_load_matches_the_embedded_table() {
    let loaded = PlaceTable::from_dir(&manifest_dir()).expect("the real manifests load");
    let embedded = PlaceTable::embedded();
    assert_eq!(loaded.viewpoints(), embedded.viewpoints());
    assert_eq!(loaded.places(), embedded.places());
    let files = std::fs::read_dir(manifest_dir()).expect("dir").filter(|e| {
        e.as_ref()
            .is_ok_and(|e| e.path().extension().is_some_and(|x| x == "json"))
    });
    assert_eq!(
        files.count(),
        loaded.viewpoints().len(),
        "one viewpoint per manifest file"
    );
    for v in loaded.viewpoints() {
        assert!(
            v.aliases.len() >= 6,
            "{} has {} aliases",
            v.id,
            v.aliases.len()
        );
        assert!(v.has_live_camera, "{} has no image_url", v.id);
    }
}

#[test]
fn every_alias_parses_to_its_own_place() {
    let parser = Parser::new(
        PlaceTable::from_dir(&manifest_dir()).expect("load"),
        TimeConfig::default(),
    );
    let now = DateTime::parse_from_rfc3339("2026-11-19T10:00:00-08:00")
        .expect("time")
        .with_timezone(&Utc);
    let mut checked = 0;
    for v in parser.places().viewpoints() {
        for alias in &v.aliases {
            for utterance in [
                format!("{alias} in an hour"),
                format!("show me {alias} tomorrow at 4"),
                format!("is {alias} out?"),
            ] {
                let q = parser
                    .parse(&utterance, &ParseContext { now, last: None })
                    .unwrap_or_else(|e| panic!("{utterance:?}: {e}"));
                assert_eq!(q.place_id, v.place_id, "{utterance:?}");
                assert_eq!(q.place_match.stage, PlaceStage::Alias, "{utterance:?}");
                assert!(
                    q.ambiguities
                        .iter()
                        .all(|a| a.field != snow_globe_core::query::AmbiguityField::Place),
                    "{utterance:?}: unexpected place chip"
                );
                checked += 1;
            }
        }
    }
    let aliases: usize = parser
        .places()
        .viewpoints()
        .iter()
        .map(|v| v.aliases.len())
        .sum();
    println!("aliases checked end to end: {aliases} aliases x 3 phrasings = {checked} utterances");
    assert_eq!(checked, 3 * aliases);
    assert!(
        aliases >= 6 * 8,
        "the schema asks for at least 6 aliases per viewpoint"
    );
}
