//! The `snow-globe-ask` and `snow-globe-tier0` binaries, run as processes over the committed
//! sidecar fixture (PRD §9.1).

mod tier0_support;

use std::process::Command;

fn run(bin: &str, args: &[&str]) -> (i32, String, String) {
    let out = Command::new(bin).args(args).output().expect("binary runs");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn ask_prints_the_frame_and_its_caption() {
    let fx = tier0_support::materialise();
    let dir = fx.path().to_string_lossy().into_owned();
    let (code, stdout, stderr) = run(
        env!("CARGO_BIN_EXE_snow-globe-ask"),
        &[
            "Is the mountain out tomorrow at 4?",
            "--now",
            "2026-11-23T10:00:00-06:00",
            "--machine-tz",
            "America/Chicago",
            "--data-dir",
            &dir,
        ],
    );
    assert_eq!(code, 0, "{stderr}");
    let v: serde_json::Value = serde_json::from_str(stdout.trim()).expect("one JSON object");
    assert_eq!(v["status"], "match");
    assert_eq!(v["query"]["viewpoint_id"], "rainier.paradise-mountain");
    assert_eq!(v["query"]["valid_time"], "2026-11-24T16:00:00-08:00");
    // Rainier's library is our own captures: the 22 Nov frame taken at 16:08 PST.
    assert_eq!(
        v["tier0"]["frame"]["key"],
        "live:rainier.paradise-mountain/20261122T000859Z_49c9b6ffe52c"
    );
    assert_eq!(v["tier0"]["frame"]["source"], "live");
    assert!(v["tier0"]["caption"]["line1"].as_str().is_some_and(|c| {
        c.starts_with("REAL PHOTO, CLOSEST MATCH · taken Sat 21 Nov, 4:08 PM PST")
    }));
    assert_eq!(v["library"]["cache"], "created");

    // Again: the index loads from its cache.
    let (_, stdout, _) = run(
        env!("CARGO_BIN_EXE_snow-globe-ask"),
        &[
            "Is the mountain out tomorrow at 4?",
            "--now",
            "2026-11-23T10:00:00-06:00",
            "--data-dir",
            &dir,
        ],
    );
    let v: serde_json::Value = serde_json::from_str(stdout.trim()).expect("json");
    assert_eq!(v["library"]["cache"], "hit");

    // No frame qualifies before the first capture: exit 3, said plainly.
    let (code, stdout, stderr) = run(
        env!("CARGO_BIN_EXE_snow-globe-ask"),
        &[
            "rainier at sunset",
            "--now",
            "2026-11-18T09:00:00-08:00",
            "--data-dir",
            &dir,
        ],
    );
    assert_eq!(code, 3, "{stdout}");
    assert!(stderr.starts_with("No real photo qualifies"), "{stderr}");

    // Usage and parse errors.
    let (code, _, _) = run(env!("CARGO_BIN_EXE_snow-globe-ask"), &[]);
    assert_eq!(code, 64);
    let (code, stdout, _) = run(
        env!("CARGO_BIN_EXE_snow-globe-ask"),
        &[
            "golden gate at 9",
            "--now",
            "2026-11-23T10:00:00-08:00",
            "--data-dir",
            &dir,
        ],
    );
    assert_eq!(code, 2);
    assert!(stdout.starts_with(r#"{"error":"no_match""#), "{stdout}");
}

#[test]
fn tier0_index_reports_the_fixture() {
    let fx = tier0_support::materialise();
    let dir = fx.path().to_string_lossy().into_owned();
    let (code, stdout, stderr) = run(
        env!("CARGO_BIN_EXE_snow-globe-tier0"),
        &["index", "--data-dir", &dir, "--no-cache"],
    );
    assert_eq!(code, 0, "{stderr}");
    let v: serde_json::Value = serde_json::from_str(stdout.trim()).expect("json");
    assert_eq!(v["entries"], 12);
    assert_eq!(v["sidecars"], 31);
    assert_eq!(v["excluded"]["held_out_archive"], 3);
    assert_eq!(v["excluded"]["sealed_window"], 2);
    assert_eq!(v["excluded"]["symlink"], 1);
    assert_eq!(v["paths_refused"]["quarantine"], 1);
    let (code, _, _) = run(env!("CARGO_BIN_EXE_snow-globe-tier0"), &["frobnicate"]);
    assert_eq!(code, 64);
}
