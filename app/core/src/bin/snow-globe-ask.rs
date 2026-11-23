//! `snow-globe-ask "<utterance>" [--now <RFC3339>] [--machine-tz <IANA>] [--context <place>] [--open]`:
//! print the closest real photo as JSON (PRD §9.1 Tier 0). See `snow_globe_core::tier0::ask`.

use std::io::Write;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = snow_globe_core::tier0::ask::run(&args);
    // Ignore broken pipes (for example `| head`).
    let _ = std::io::stdout().write_all(out.stdout.as_bytes());
    let _ = std::io::stderr().write_all(out.stderr.as_bytes());
    ExitCode::from(u8::try_from(out.code).unwrap_or(1))
}
