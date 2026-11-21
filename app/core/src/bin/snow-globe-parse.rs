//! `snow-globe-parse "<utterance>" --now <RFC3339> --machine-tz <IANA>`: print the query JSON
//! (PRD §7.3). See `snow_globe_core::cli` for the options.

use std::io::Write;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = snow_globe_core::cli::run(&args);
    // Ignore broken pipes (for example `| head`).
    let _ = std::io::stdout().write_all(out.stdout.as_bytes());
    let _ = std::io::stderr().write_all(out.stderr.as_bytes());
    ExitCode::from(u8::try_from(out.code).unwrap_or(1))
}
