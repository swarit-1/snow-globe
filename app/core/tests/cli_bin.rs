//! The `snow-globe-parse` binary itself (PRD §7.3): run as a process, as a script would.

use std::process::Command;

fn run(args: &[&str]) -> (i32, String, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_snow-globe-parse"))
        .args(args)
        .output()
        .expect("binary runs");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn prints_the_prd_example() {
    let (code, stdout, stderr) = run(&[
        "Is Rainier out tomorrow at 4?",
        "--now",
        "2026-11-19T12:00:00-06:00",
        "--machine-tz",
        "America/Chicago",
    ]);
    assert_eq!(code, 0, "{stderr}");
    let v: serde_json::Value = serde_json::from_str(stdout.trim()).expect("one JSON object");
    assert_eq!(v["valid_time"], "2026-11-20T16:00:00-08:00");
    assert_eq!(v["valid_time_utc"], "2026-11-21T00:00:00Z");
    assert_eq!(v["time_source"]["rule"], "day-plus-bare-hour");
    assert_eq!(
        v["ambiguities"][0]["alternative"],
        "2026-11-20T04:00:00-08:00"
    );
    assert_eq!(v["confidence"], 0.97);
}

#[test]
fn wall_clock_now_is_read_in_the_machine_zone() {
    // 10:00 Chicago wall time = 08:00 at Paradise; "at 9" is then 09:00 there.
    let (code, stdout, _) = run(&[
        "rainier at 9",
        "--now",
        "2026-11-19T10:00",
        "--machine-tz",
        "America/Chicago",
    ]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains(r#""valid_time":"2026-11-19T09:00:00-08:00""#),
        "{stdout}"
    );
}

#[test]
fn errors_and_usage() {
    let (code, stdout, _) = run(&["Golden Gate at 9", "--now", "2026-11-19T12:00:00-06:00"]);
    assert_eq!(code, 2);
    let v: serde_json::Value = serde_json::from_str(stdout.trim()).expect("json");
    assert_eq!(v["error"], "no_match");
    assert_eq!(v["closest"].as_array().map(Vec::len), Some(3));
    let (code, _, stderr) = run(&["rainier", "--machine-tz"]);
    assert_eq!(code, 64, "{stderr}");
    let (code, stdout, _) = run(&["--help"]);
    assert_eq!(code, 0);
    assert!(stdout.starts_with("usage: snow-globe-parse"));
    let (code, stdout, _) = run(&[
        "rainier at 9",
        "--now",
        "2026-11-19T12:00:00-06:00",
        "--pretty",
    ]);
    assert_eq!(code, 0);
    assert!(stdout.contains("\n  \"place_id\": \"rainier\""), "{stdout}");
}
