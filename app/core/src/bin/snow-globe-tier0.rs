//! `snow-globe-tier0 index|bench`: the Tier 0 library report and the measured run (PRD §9.1,
//! §10.4). See `snow_globe_core::tier0::tool`.

use std::io::Write;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = snow_globe_core::tier0::tool::run(&args);
    // Ignore broken pipes (for example `| head`).
    let _ = std::io::stdout().write_all(out.stdout.as_bytes());
    let _ = std::io::stderr().write_all(out.stderr.as_bytes());
    ExitCode::from(u8::try_from(out.code).unwrap_or(1))
}
